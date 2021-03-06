import os
import tensorflow as tf
from tensorflow.keras.layers import (Dense,
                                     Dropout)
from transformers import TFBertModel
from time import time
print('TensorFlow:', tf.__version__)

try:
    tpu = tf.distribute.cluster_resolver.TPUClusterResolver(
        'srihari-1-tpu')
    print('Running on TPU ', tpu.cluster_spec().as_dict()['worker'])
except ValueError:
    tpu = None

if tpu:
    tf.config.experimental_connect_to_cluster(tpu)
    tf.tpu.experimental.initialize_tpu_system(tpu)
    strategy = tf.distribute.experimental.TPUStrategy(tpu)
else:
    strategy = tf.distribute.MirroredStrategy()

print("REPLICAS: ", strategy.num_replicas_in_sync)

batch_size = 32 * strategy.num_replicas_in_sync
embedding_dim = 512
autotune = tf.data.experimental.AUTOTUNE

train_steps = 1262996 * 0.8 // batch_size
val_steps = train_steps // 10
epochs = 100
print('Batch Size:', batch_size)

config_name = 'model_a'
base_dir = 'gs://tfworld/hparams_search'
model_dir = os.path.join(base_dir, config_name)
tensorboard_dir = os.path.join(model_dir, 'logs_' + str(time()))
tfrecords_pattern_train = os.path.join(base_dir, 'tfrecords', 'train*')
tfrecords_pattern_val = os.path.join(base_dir, 'tfrecords', 'eval*')

print('Logging in: ', tensorboard_dir)

features = {
    'title': tf.io.FixedLenFeature([512], dtype=tf.int64),
    'citation': tf.io.FixedLenFeature([512], dtype=tf.float32),
}


def parse_example(example_proto):
    parsed_example = tf.io.parse_single_example(example_proto, features)
    title = parsed_example['title']
    citation = parsed_example['citation']

    title = tf.cast(title, dtype=tf.int32)
    citation = tf.cast(citation, dtype=tf.float32)
    return (title, citation), tf.constant([1.0], dtype=tf.float32)


with strategy.scope():
    train_files = tf.data.Dataset.list_files(tfrecords_pattern_train)
    train_dataset = train_files.interleave(tf.data.TFRecordDataset,
                                           cycle_length=32,
                                           block_length=4,
                                           num_parallel_calls=autotune)
    train_dataset = train_dataset.map(
        parse_example, num_parallel_calls=autotune)
    train_dataset = train_dataset.batch(batch_size, drop_remainder=True)
    train_dataset = train_dataset.repeat()
    train_dataset = train_dataset.prefetch(autotune)

    val_files = tf.data.Dataset.list_files(tfrecords_pattern_val)
    val_dataset = val_files.interleave(tf.data.TFRecordDataset,
                                       cycle_length=32,
                                       block_length=4,
                                       num_parallel_calls=autotune)
    val_dataset = val_dataset.map(parse_example, num_parallel_calls=autotune)
    val_dataset = val_dataset.batch(batch_size, drop_remainder=True)
    val_dataset = val_dataset.repeat()
    val_dataset = val_dataset.prefetch(autotune)


@tf.function
def loss_fn(_, probs):
    '''
        1. Every sample is its own positive, and  the rest of the
            elements in the batch are its negative.
        2. Each TPU core gets 1/8 * global_batch_size elements, hence
            compute shape dynamically.
        3. Dataset produces dummy labels to make sure the loss_fn matches
            the loss signature of keras, actual labels are computed inside this
            function.
        4. `probs` lie in [0, 1] and are to be treated as probabilities.
    '''
    bs = tf.shape(probs)[0]
    labels = tf.eye(bs, bs)
    return tf.losses.categorical_crossentropy(labels,
                                              probs,
                                              from_logits=False)


def create_model(drop_out, dense_units, activation):
    textIds = tf.keras.Input(
        shape=(512,), dtype=tf.int32)    # from bert tokenizer
    # normalized word2vec outputs
    citation = tf.keras.Input(shape=(512,))

    bert_model = TFBertModel.from_pretrained(
        'scibert_scivocab_uncased', from_pt=True)

    textOut = bert_model(textIds)
    textOutMean = tf.reduce_mean(textOut[0], axis=1)
    textOutSim = Dense(units=embedding_dim, activation=activation,
                       name='DenseTitle')(textOutMean)
    textOutSim = Dropout(drop_out)(textOutSim)

    citationSim = citation
    for units in dense_units:
      citationSim = Dense(units=units, activation=activation,
                          name='DenseCitation')(citationSim)
      citationSim = Dropout(drop_out)(citationSim)

    # Get dot product of each of title x citation combinations
    dotProduct = tf.reduce_sum(tf.multiply(
        textOutSim[:, None, :], citationSim), axis=-1)

    # Softmax to make sure each row has sum == 1.0
    probs = tf.nn.softmax(dotProduct, axis=-1)

    model = tf.keras.Model(inputs=[textIds, citation], outputs=[probs])
    return model

config = {
  'drop_out':0.2,
  'dense_units':[512, 512],
  'activation':'tanh'
}
with strategy.scope():
    model = create_model(**config)
    model.compile(loss=loss_fn,
                  optimizer=tf.keras.optimizers.Adam(learning_rate=3e-5))

callbacks = [tf.keras.callbacks.TensorBoard(log_dir=tensorboard_dir,
                                            update_freq='epoch'),
             tf.keras.callbacks.ModelCheckpoint(filepath=model_dir + '/epoch_{epoch:02d}_{loss:.2f}',
                                                monitor='loss',
                                                verbose=1,
                                                save_weights_only=True,
                                                save_freq='epoch')
             ]

model.fit(train_dataset,
          epochs=epochs,
          steps_per_epoch=train_steps,
          validation_data=val_dataset,
          validation_steps=val_steps,
          validation_freq=1,
          callbacks=callbacks)
