import tensorflow as tf
import os
import random
import ssbm
import ctypes
import tf_lib as tfl
import util
import ctype_util as ct
import numpy as np
import embed
from default import *
from dqn import DQN
from ac import ActorCritic
#from thompson_dqn import ThompsonDQN
from operator import add, sub
from enum import Enum
from reward import computeRewards
#from rac import RecurrentActorCritic

class Mode(Enum):
  TRAIN = 0
  PLAY = 1

models = [
  DQN,
  ActorCritic,
  #ThompsonDQN,
  #RecurrentActorCritic,
]
models = {model.__name__ : model for model in models}

class RLConfig(Default):
  _options = [
    Option('tdN', type=int, default=5, help="use n-step TD error"),
    Option('reward_halflife', type=float, default=2.0, help="time to discount rewards by half, in seconds"),
    Option('act_every', type=int, default=3, help="Take an action every ACT_EVERY frames."),
    Option('experience_time', type=int, default=60, help="Length of experiences, in seconds."),
  ]
  
  def __init__(self, **kwargs):
    super(RLConfig, self).__init__(**kwargs)
    self.fps = 60 // self.act_every
    self.discount = 0.5 ** ( 1.0 / (self.fps*self.reward_halflife) )
    self.experience_length = self.experience_time * self.fps

class Model(Default):
  _options = [
    Option('model', type=str, default="DQN", choices=models.keys()),
    Option('path', type=str, help="path to saved model"),
    Option('gpu', type=bool, default=False, help="train on gpu"),
    Option('memory', type=int, default=0, help="number of frames to remember"),
    Option('name', type=str)
  ]
  
  _members = [
    ('rlConfig', RLConfig),
    ('embedGame', embed.GameEmbedding),
  ]
  
  def __init__(self, mode = Mode.TRAIN, debug = False, **kwargs):
    Default.__init__(self, init_members=False, **kwargs)
    
    if self.name is None:
      self.name = self.model
    
    if self.path is None:
      self.path = "saves/%s/" % self.name
    
    print("Creating model:", self.model)
    modelType = models[self.model]
    
    self.graph = tf.Graph()
    
    device = '/gpu:0' if self.gpu else '/cpu:0'
    print("Using device " + device)
    
    if not self.gpu:
      os.environ['CUDA_VISIBLE_DEVICES'] = ""
    
    with self.graph.as_default(), tf.device(device):
      self._init_members(**kwargs)
      
      self.global_step = tf.Variable(0, name='global_step', trainable=False)
      
      state_size = self.embedGame.size
      
      history_size = (1+self.memory) * (state_size+embed.action_size)
      self.model = modelType(history_size, embed.action_size, self.global_step, self.rlConfig, **kwargs)

      #self.variables = self.model.getVariables() + [self.global_step]
      
      if mode == Mode.TRAIN:
        with tf.name_scope('train'):
          self.experience = ct.inputCType(ssbm.SimpleStateAction, [None, self.rlConfig.experience_length], "experience")
          # instantaneous rewards for all but the first state
          self.experience['reward'] = tf.placeholder(tf.float32, [None, None], name='reward')
          mean_reward = tf.reduce_mean(self.experience['reward'])
          
          states = self.embedGame(self.experience['state'])
          
          prev_actions = embed.embedAction(self.experience['prev_action'])
          states = tf.concat(2, [states, prev_actions])
          
          train_length = self.rlConfig.experience_length - self.memory
          
          history = [tf.slice(states, [0, i, 0], [-1, train_length, -1]) for i in range(self.memory+1)]
          self.train_states = tf.concat(2, history)
          
          actions = embed.embedAction(self.experience['action'])
          self.train_actions = tf.slice(actions, [0, self.memory, 0], [-1, train_length, -1])
          
          self.train_rewards = tf.slice(self.experience['reward'], [0, self.memory], [-1, -1])
          
          """
          data_names = ['state', 'action', 'reward']
          self.saved_data = [tf.get_session_handle(getattr(self, 'train_%ss' % name)) for name in data_names]
          
          self.placeholders = []
          loaded_data = []
          
          for name in data_names:
            placeholder, data = tf.get_session_tensor(tf.float32)
            self.placeholders.append(placeholder)
            #data = tf.reshape(data, tf.shape(getattr(self, 'embedded_%ss' % name)))
            loaded_data.append(data)
          
          loss, stats = self.model.getLoss(*loaded_data, **kwargs)
          """
          
          self.train_op = self.model.train(self.train_states, self.train_actions, self.train_rewards)

          #tf.scalar_summary("loss", loss)
          #tf.scalar_summary('learning_rate', tf.log(self.learning_rate))
          tf.scalar_summary('reward', mean_reward)
          merged = tf.merge_all_summaries()
          
          increment = tf.assign_add(self.global_step, 1)
          
          misc = tf.group(increment)
          
          self.run_dict = dict(summary=merged, global_step=self.global_step, train=self.train_op, misc=misc)
          
          print("Creating summary writer at logs/%s." % self.name)
          self.writer = tf.train.SummaryWriter('logs/' + self.name, self.graph)
      else:
        with tf.name_scope('policy'):
          self.input = ct.inputCType(ssbm.SimpleStateAction, [self.memory+1], "input")
          states = self.embedGame(self.input['state'])
          prev_actions = embed.embedAction(self.input['prev_action'])
          
          history = tf.concat(1, [states, prev_actions])
          history = tf.reshape(history, [history_size])
          
          self.policy = self.model.getPolicy(history)
      
      tf_config = dict(
        allow_soft_placement=True,
        #log_device_placement=True,
      )
      
      if mode == Mode.PLAY: # don't eat up cpu cores
        tf_config.update(
          inter_op_parallelism_threads=1,
          intra_op_parallelism_threads=1,
        )
      else:
        tf_config.update(
          #gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.3),
        )
      
      self.sess = tf.Session(
        graph=self.graph,
        config=tf.ConfigProto(**tf_config),
      )
      
      self.debug = debug
      
      #self.saver = tf.train.Saver(self.variables)
      print("Creating saver.")
      self.saver = tf.train.Saver(tf.all_variables())

  def act(self, history, verbose=False):
    feed_dict = dict(util.deepValues(util.deepZip(self.input, ct.vectorizeCTypes(ssbm.SimpleStateAction, history))))
    return self.model.act(self.sess.run(self.policy, feed_dict), verbose)

  #summaryWriter = tf.train.SummaryWriter('logs/', sess.graph)
  #summaryWriter.flush()

  def debugGrads(self, feed_dict):
    gs = self.sess.run([gv[0] for gv in self.grads_and_vars], feed_dict)
    vs = self.sess.run([gv[1] for gv in self.grads_and_vars], feed_dict)
    #   loss = sess.run(qLoss, feed_dict)
    #act_qs = sess.run(qs, feed_dict)
    #act_qs = list(map(util.compose(np.sort, np.abs), act_qs))

    #t = sess.run(temperature)
    #print("Temperature: ", t)
    #for i, act in enumerate(act_qs):
    #  print("act_%d"%i, act)
    #print("grad/param(action)", np.mean(np.abs(gs[0] / vs[0])))
    #print("grad/param(stage)", np.mean(np.abs(gs[2] / vs[2])))

    print("param avg and max")
    for g, v in zip(gs, vs):
      abs_v = np.abs(v)
      abs_g = np.abs(g)
      print(v.shape, np.mean(abs_v), np.max(abs_v), np.mean(abs_g), np.max(abs_g))

    print("grad/param avg and max")
    for g, v in zip(gs, vs):
      ratios = np.abs(g / v)
      print(np.mean(ratios), np.max(ratios))
    #print("grad", np.mean(np.abs(gs[4])))
    #print("param", np.mean(np.abs(vs[0])))

    # if step_index == 10:
    import ipdb; ipdb.set_trace()

  def train(self, experiences, batch_steps=1, **kwargs):
    #state_actions = ssbm.readStateActions(filename)
    #feed_dict = feedStateActions(state_actions)
    #experiences = util.async_map(ssbm.readStateActions_pickle, filenames)()
    experiences = util.deepZip(*experiences)
    experiences = util.deepMap(np.array, experiences)
    
    input_dict = dict(util.deepValues(util.deepZip(self.experience, experiences)))
    
    """
    saved_data = self.sess.run(self.saved_data, input_dict)
    handles = [t.handle for t in saved_data]
    
    saved_dict = dict(zip(self.placeholders, handles))
    """

    if self.debug:
      self.debugGrads(input_dict)
    
    for _ in range(batch_steps):
      results = self.sess.run(self.run_dict, input_dict)
      
      summary_str = results['summary']
      global_step = results['global_step']
      self.writer.add_summary(summary_str, global_step)

  def save(self):
    import os
    util.makedirs(self.path)
    print("Saving to", self.path)
    self.saver.save(self.sess, self.path + "snapshot")

  def restore(self):
    print("Restoring from", self.path)
    self.saver.restore(self.sess, self.path + "snapshot")

  def init(self):
    with self.graph.as_default():
      #self.sess.run(tf.initialize_variables(self.variables))
      self.sess.run(tf.initialize_all_variables())

