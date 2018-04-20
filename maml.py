import numpy as np
import tensorflow as tf
from tensorflow import flags

FLAGS = flags.FLAGS


class MAML:
	def __init__(self, dim_input, dim_output, test_num_updates):
		"""

		:param dim_input:
		:param dim_output:
		:param test_num_updates:
		"""
		self.dim_input = dim_input
		self.dim_output = dim_output
		self.test_num_updates = test_num_updates

		self.meta_lr = tf.placeholder_with_default(FLAGS.meta_lr, ())

		self.imgsz = int(np.sqrt(self.dim_input / 3))
		print('imgsz:', self.imgsz)

	def build(self, input, prefix='metatrain'):
		"""

		:param input:
		:param prefix:
		:return:
		"""
		# support_x : [4, 1*5, 84*84*3]
		# query_x   : [4, 15*5, 84*84*3]
		# support_y : [4, 5, 5]
		# query_y   : [4, 15*5, 5]
		self.support_x  = input['support_x']
		self.support_y  = input['support_y']
		self.query_x    = input['query_x']
		self.query_y    = input['query_y']

		# train iteration
		# according to the paper, we train use 5 steps while test use 10 steps.
		# which means, when building meta-(train/test) graph, we need repeat network by 5 times,
		# however, when build test graph, we need repeat 10 times.
		K = max(self.test_num_updates, FLAGS.train_iteration)
		print('building ', prefix, 'update steps:', K)
		# num of tasks
		N = tf.to_float(FLAGS.meta_batchsz)

		# since we need to construct train model and test model, this function will be used for twice
		# if 'weights' in dir(self):
		# 	scope.reuse_variables()
		# 	weights = self.weights
		# 	print(prefix, 'reuse weights.')
		# else:
		# 	# build the weights
		# 	self.weights = weights = self.conv_weights()
		# 	print(prefix, 'build weights.')
		self.weights = weights = self.conv_weights()

		# the following list save all tasks' op.
		support_pred_tasks, support_loss_tasks, support_acc_tasks = [], [], []
		query_preds_tasks, query_losses_tasks, query_accs_tasks = [[]] * K, [[]] * K, [[]] * K

		def meta_task(input):
			"""

			:param input:
			:param reuse:
			:return:
			"""
			support_x, query_x, support_y, query_y = input
			# to record the op in t update step.
			query_preds, query_losses, query_accs = [], [], []

			# forward: support_x -> 4conv -> fc -> [5]
			# NOTICE: reuse=False on unused_op to create batch_norm tenors and then reuse these batch_norm tensors
			# on formal meta_op
			# However, we build two graphs totally: metatrain & metaeval, and these 2 graphs does NOT share batch_norm
			# tensors actually
			# metatrain : create weights
			# metatrain : reuse=False
			# metatrain : reuse=True
			# metaval   : reuse weights
			# metaval   : reuse=False
			# metaval   : reuse=True
			# ==================================
			# REUSE       True        False
			# Not exist   Error       Create one
			# Existed     reuse       Error
			# ==================================
			# That's, to create variable, you must turn off reuse
			support_pred = self.forward(support_x, weights)  # only reuse on the first iter
			support_loss = tf.nn.softmax_cross_entropy_with_logits(logits=support_pred, labels=support_y)
			# compute gradients
			grads = tf.gradients(support_loss, list(weights.values()))
			# grad and variable dict
			gvs = dict(zip(weights.keys(), grads))

			#map(lambda gv: [tf.clip_by_value(gv[0], -10., 10.), gv[1]], grads_vars_G)
			# theta_pi = theta - alpha * grads
			fast_weights = dict(zip(weights.keys(), [weights[key] - FLAGS.train_lr * gvs[key] for key in weights.keys()]))
			# use theta_pi to forward meta-test
			query_pred = self.forward(query_x, fast_weights)
			# meta-test loss
			query_loss = tf.nn.softmax_cross_entropy_with_logits(logits=query_pred, labels=query_y)
			# record T0 pred and loss for meta-test
			query_preds.append(query_pred)
			query_losses.append(query_loss)

			# continue to build T1-TK steps graph
			for _ in range(1, K):
				# T_k loss on meta-train
				# we need meta-train loss to fine-tune the task and meta-test loss to update theta
				loss = tf.nn.softmax_cross_entropy_with_logits(logits=self.forward(support_x, fast_weights),
				                                               labels=support_y)
				# compute gradients
				grads = tf.gradients(loss, list(fast_weights.values()))
				# compose grad and variable dict
				gvs = dict(zip(fast_weights.keys(), grads))
				# update theta_pi according to varibles
				fast_weights = dict(zip(fast_weights.keys(), [fast_weights[key] - FLAGS.train_lr * gvs[key]
				                         for key in fast_weights.keys()]))
				# forward on theta_pi
				query_pred = self.forward(query_x, fast_weights)
				# we need accumulate all meta-test losses to update theta
				query_loss = tf.nn.softmax_cross_entropy_with_logits(logits=query_pred, labels=query_y)
				query_preds.append(query_pred)
				query_losses.append(query_loss)

			# actually, this is the T0 step's accuracy on support set
			support_acc = tf.contrib.metrics.accuracy(tf.argmax(tf.nn.softmax(support_pred), 1),
			                                             tf.argmax(support_y, 1))
			# compute every steps' accuracy on query set, we may notice the query_acc increase due to we have
			# backpropagated by support set
			for i in range(K):
				query_accs.append(tf.contrib.metrics.accuracy(tf.argmax(tf.nn.softmax(query_preds[i]), 1),
					                                            tf.argmax(query_y, 1)))
			# we just use the first step support op: support_pred & support_loss, but igonre these support op
			# at step 1:K-1.
			# however, we return all pred&loss&acc op at each time steps.
			result = [support_pred, support_loss, support_acc, query_preds, query_losses, query_accs]

			return result

		# support_x : [4, 1*5, 84*84*3]
		# query_x   : [4, 15*5, 84*84*3]
		# support_y : [4, 5, 5]
		# query_y   : [4, 15*5, 5]
		# return: [support_pred, support_loss, support_acc, query_preds, query_losses, query_accs]
		out_dtype = [tf.float32, tf.float32, tf.float32, [tf.float32] * K, [tf.float32] * K, [tf.float32] * K]
		result = tf.map_fn(meta_task, elems=(self.support_x, self.query_x, self.support_y, self.query_y),
		                   dtype=out_dtype, parallel_iterations=FLAGS.meta_batchsz)
		support_pred_tasks, support_loss_tasks, support_acc_tasks, \
			query_preds_tasks, query_losses_tasks, query_accs_tasks = result


		## Performance & Optimization
		# the following code is not in Variable_scope now.
		if 'train' in prefix:

			# no need to average
			# self.support_pred_tasks, self.query_preds_tasks = support_pred_tasks, query_preds_tasks

			# average loss
			self.support_loss = support_loss = tf.reduce_sum(support_loss_tasks) / N
			# [avgloss_t1, avgloss_t2, ..., avgloss_K]
			self.query_losses = query_losses = [tf.reduce_sum(query_losses_tasks[j]) / N
			                                        for j in range(K)]
			# average accuracy
			self.support_acc = support_acc = tf.reduce_sum(support_acc_tasks) / N
			# average accuracies
			self.query_accs = query_accs = [tf.reduce_sum(query_accs_tasks[j]) / N
			                                        for j in range(K)]
			# if needing extra pretrain, we just use the op, it's very simple classification network.
			# self.pretrain_op = tf.train.AdamOptimizer(self.meta_lr, name='pretrain_optim').minimize(support_loss)

			# add batch_norm ops before meta_op
			update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
			with tf.control_dependencies(update_ops):
				# TODO: the update_ops must be put before tf.train.AdamOptimizer,
				# otherwise it throws Not in same Frame Error.
				meta_loss = tf.identity(self.query_losses[-1])

			# meta-train optim
			optimizer = tf.train.AdamOptimizer(self.meta_lr, name='meta_optim')
			# meta-train gradients, query_losses[-1] is the accumulated loss across over tasks.
			gvs = optimizer.compute_gradients(meta_loss)
			# meta-train grads clipping
			gvs = [(tf.clip_by_value(grad, -10, 10), var) for grad, var in gvs]
			# update theta
			self.meta_op = optimizer.apply_gradients(gvs)

		else: # test

			# average loss
			self.test_support_loss = support_loss = tf.reduce_sum(support_loss_tasks) / N
			# [avgloss_t1, avgloss_t2, ..., avgloss_K]
			self.test_query_losses = query_losses = [tf.reduce_sum(query_losses_tasks[j]) / N
			                                        for j in range(K)]
			# average accuracy
			self.test_support_acc = support_acc = tf.reduce_sum(support_acc_tasks) / N
			# average accuracies
			self.test_query_accs = query_accs = [tf.reduce_sum(query_accs_tasks[j]) / N
			                                        for j in range(K)]

		# Summaries
		# NOTICE: every time build model, support_loss will be added to the summary, but it's different.
		tf.summary.scalar(prefix + 'support loss', support_loss)
		tf.summary.scalar(prefix + 'support acc', support_acc)
		for j in range(K):
			tf.summary.scalar(prefix + 'query loss, step ' + str(j + 1), query_losses[j])
			tf.summary.scalar(prefix + 'query acc, step ' + str(j + 1), query_accs[j])




	def conv_weights(self):
		weights = {}
 
		conv_initializer = tf.contrib.layers.xavier_initializer_conv2d()
		fc_initializer = tf.contrib.layers.xavier_initializer()
		k = 3

		with tf.variable_scope('MAML', reuse= tf.AUTO_REUSE):
			weights['conv1']    = tf.get_variable('conv1w', [k, k, 3, 32],  initializer=conv_initializer)
			weights['b1']       = tf.get_variable('conv1b', initializer=tf.zeros([32]))
			weights['conv2']    = tf.get_variable('conv2w', [k, k, 32, 32], initializer=conv_initializer)
			weights['b2']       = tf.get_variable('conv2b', initializer=tf.zeros([32]))
			weights['conv3']    = tf.get_variable('conv3w', [k, k, 32, 32], initializer=conv_initializer)
			weights['b3']       = tf.get_variable('conv3b', initializer=tf.zeros([32]))
			weights['conv4']    = tf.get_variable('conv4w', [k, k, 32, 32], initializer=conv_initializer)
			weights['b4']       = tf.get_variable('conv4b', initializer=tf.zeros([32]))

			# assumes max pooling
			weights['w5']       = tf.get_variable('fc1w', [32 * 5 * 5, FLAGS.nway], initializer=fc_initializer)
			weights['b5']       = tf.get_variable('fc1b', initializer=tf.zeros([self.dim_output]))


			return weights

	def conv_block(self, x, weight, bias, scope):
		"""
		build a block with conv2d->batch_norm->pooling
		:param x:
		:param weight:
		:param bias:
		:param reuse:
		:param scope:
		:return:
		"""
		# conv
		x = tf.nn.conv2d(x, weight, [1, 1, 1, 1], 'SAME', name=scope + '_conv2d') + bias
		# batch norm, activation_fn=tf.nn.relu,
		# NOTICE: must have tf.layers.batch_normalization
		# x = tf.contrib.layers.batch_norm(x, activation_fn=tf.nn.relu)
		with tf.variable_scope('MAML'):
			# train is set to True ALWAYS, please refer to https://github.com/cbfinn/maml/issues/9
			x = tf.layers.batch_normalization(x, training=FLAGS.train, name=scope + '_bn', reuse=tf.AUTO_REUSE)
		# relu
		x = tf.nn.relu(x, name=scope + '_relu')
		# pooling
		x = tf.nn.max_pool(x, [1, 2, 2, 1], [1, 2, 2, 1], 'VALID', name=scope + '_pool')
		return x


	def forward(self, x, weights):
		"""


		:param x:
		:param weights:
		:param reuse: reuse is for the normalization parameters.
		:param scope:
		:return:
		"""
		# [b, 84, 84, 3]
		x = tf.reshape(x, [-1, self.imgsz, self.imgsz, 3], name='reshape1')

		hidden1 = self.conv_block(x,        weights['conv1'], weights['b1'], 'conv0')
		hidden2 = self.conv_block(hidden1,  weights['conv2'], weights['b2'], 'conv1')
		hidden3 = self.conv_block(hidden2,  weights['conv3'], weights['b3'], 'conv2')
		hidden4 = self.conv_block(hidden3,  weights['conv4'], weights['b4'], 'conv3')

		# get_shape is static shape, (5, 5, 5, 32)
		# print('flatten:', hidden4.get_shape())
		# flatten layer
		hidden4 = tf.reshape(hidden4, [-1, np.prod([int(dim) for dim in hidden4.get_shape()[1:]])], name='reshape2')

		output = tf.add(tf.matmul(hidden4, weights['w5']), weights['b5'], name='fc')

		return output

	def forward2(self, x):
		# NOTICE: we will optimize these variable name start with weights*
		with tf.variable_scope('MAML2', reuse=tf.AUTO_REUSE):
			x = tf.reshape(x, [-1, self.imgsz, self.imgsz, 3], name='reshape1')
			# conv1
			x = tf.layers.conv2d(x, filters=32, kernel_size=[3, 3], name='weights1',
			                     kernel_initializer=tf.contrib.layers.xavier_initializer_conv2d())

			x = tf.layers.batch_normalization(x, training=FLAGS.train, name='conv1_bn')
			x = tf.nn.max_pool(x, [1, 2, 2, 1], [1, 2, 2, 1], 'VALID', name='conv1_pool')
			# conv2
			x = tf.layers.conv2d(x, filters=32, kernel_size=[3, 3], name='weights2',
			                     kernel_initializer=tf.contrib.layers.xavier_initializer_conv2d())
			x = tf.layers.batch_normalization(x, training=FLAGS.train, name='conv2_bn')
			x = tf.nn.max_pool(x, [1, 2, 2, 1], [1, 2, 2, 1], 'VALID', name='conv2_pool')
			# conv3
			x = tf.layers.conv2d(x, filters=32, kernel_size=[3, 3], name='weights3',
			                     kernel_initializer=tf.contrib.layers.xavier_initializer_conv2d())
			x = tf.layers.batch_normalization(x, training=FLAGS.train, name='conv3_bn')
			x = tf.nn.max_pool(x, [1, 2, 2, 1], [1, 2, 2, 1], 'VALID', name='conv3_pool')
			# conv4
			x = tf.layers.conv2d(x, filters=32, kernel_size=[3, 3], name='weights4',
			                     kernel_initializer=tf.contrib.layers.xavier_initializer_conv2d())
			x = tf.layers.batch_normalization(x, training=FLAGS.train, name='conv3_bn')
			x = tf.nn.max_pool(x, [1, 2, 2, 1], [1, 2, 2, 1], 'VALID', name='conv3_pool')
			# flatten
			x = tf.layers.flatten(x, name='flatten2')
			# dense
			x = tf.layers.dense(x, FLAGS.nway, name='weights5')

			return x

