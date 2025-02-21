"""Adapted from A3CTFPolicy to add V-trace.

Keep in sync with changes to A3CTFPolicy and VtraceSurrogatePolicy."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import logging
import gym

import ray
from ray.rllib.agents.impala import vtrace
from ray.rllib.models.action_dist import Categorical
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.policy.tf_policy_template import build_tf_policy
from ray.rllib.policy.tf_policy import LearningRateSchedule, \
    EntropyCoeffSchedule
from ray.rllib.utils.explained_variance import explained_variance
from ray.rllib.utils import try_import_tf

tf = try_import_tf()

logger = logging.getLogger(__name__)

BEHAVIOUR_LOGITS = "behaviour_logits"


class VTraceLoss(object):
    def __init__(self,
                 actions,
                 actions_logp,
                 actions_entropy,
                 dones,
                 behaviour_logits,
                 target_logits,
                 discount,
                 rewards,
                 values,
                 bootstrap_value,
                 dist_class,
                 valid_mask,
                 vf_loss_coeff=0.5,
                 entropy_coeff=0.01,
                 clip_rho_threshold=1.0,
                 clip_pg_rho_threshold=1.0):
        """Policy gradient loss with vtrace importance weighting.

        VTraceLoss takes tensors of shape [T, B, ...], where `B` is the
        batch_size. The reason we need to know `B` is for V-trace to properly
        handle episode cut boundaries.

        Args:
            actions: An int|float32 tensor of shape [T, B, ACTION_SPACE].
            actions_logp: A float32 tensor of shape [T, B].
            actions_entropy: A float32 tensor of shape [T, B].
            dones: A bool tensor of shape [T, B].
            behaviour_logits: A list with length of ACTION_SPACE of float32
                tensors of shapes
                [T, B, ACTION_SPACE[0]],
                ...,
                [T, B, ACTION_SPACE[-1]]
            target_logits: A list with length of ACTION_SPACE of float32
                tensors of shapes
                [T, B, ACTION_SPACE[0]],
                ...,
                [T, B, ACTION_SPACE[-1]]
            discount: A float32 scalar.
            rewards: A float32 tensor of shape [T, B].
            values: A float32 tensor of shape [T, B].
            bootstrap_value: A float32 tensor of shape [B].
            dist_class: action distribution class for logits.
            valid_mask: A bool tensor of valid RNN input elements (#2992).
        """

        # Compute vtrace on the CPU for better perf.
        with tf.device("/cpu:0"):
            self.vtrace_returns = vtrace.multi_from_logits(
                behaviour_policy_logits=behaviour_logits,
                target_policy_logits=target_logits,
                actions=tf.unstack(actions, axis=2),
                discounts=tf.to_float(~dones) * discount,
                rewards=rewards,
                values=values,
                bootstrap_value=bootstrap_value,
                dist_class=dist_class,
                clip_rho_threshold=tf.cast(clip_rho_threshold, tf.float32),
                clip_pg_rho_threshold=tf.cast(clip_pg_rho_threshold,
                                              tf.float32))
            self.value_targets = self.vtrace_returns.vs

        # The policy gradients loss
        self.pi_loss = -tf.reduce_sum(
            tf.boolean_mask(actions_logp * self.vtrace_returns.pg_advantages,
                            valid_mask))

        # The baseline loss
        delta = tf.boolean_mask(values - self.vtrace_returns.vs, valid_mask)
        self.vf_loss = 0.5 * tf.reduce_sum(tf.square(delta))

        # The entropy loss
        self.entropy = tf.reduce_sum(
            tf.boolean_mask(actions_entropy, valid_mask))

        # The summed weighted loss
        self.total_loss = (self.pi_loss + self.vf_loss * vf_loss_coeff -
                           self.entropy * entropy_coeff)


def _make_time_major(policy, tensor, drop_last=False):
    """Swaps batch and trajectory axis.

    Arguments:
        policy: Policy reference
        tensor: A tensor or list of tensors to reshape.
        drop_last: A bool indicating whether to drop the last
        trajectory item.

    Returns:
        res: A tensor with swapped axes or a list of tensors with
        swapped axes.
    """
    if isinstance(tensor, list):
        return [_make_time_major(policy, t, drop_last) for t in tensor]

    if policy.state_in:
        B = tf.shape(policy.seq_lens)[0]
        T = tf.shape(tensor)[0] // B
    else:
        # Important: chop the tensor into batches at known episode cut
        # boundaries. TODO(ekl) this is kind of a hack
        T = policy.config["sample_batch_size"]
        B = tf.shape(tensor)[0] // T
    rs = tf.reshape(tensor, tf.concat([[B, T], tf.shape(tensor)[1:]], axis=0))

    # swap B and T axes
    res = tf.transpose(
        rs, [1, 0] + list(range(2, 1 + int(tf.shape(tensor).shape[0]))))

    if drop_last:
        return res[:-1]
    return res


def build_vtrace_loss(policy, batch_tensors):
    if isinstance(policy.action_space, gym.spaces.Discrete):
        is_multidiscrete = False
        output_hidden_shape = [policy.action_space.n]
    elif isinstance(policy.action_space,
                    gym.spaces.multi_discrete.MultiDiscrete):
        is_multidiscrete = True
        output_hidden_shape = policy.action_space.nvec.astype(np.int32)
    else:
        is_multidiscrete = False
        output_hidden_shape = 1

    def make_time_major(*args, **kw):
        return _make_time_major(policy, *args, **kw)

    actions = batch_tensors[SampleBatch.ACTIONS]
    dones = batch_tensors[SampleBatch.DONES]
    rewards = batch_tensors[SampleBatch.REWARDS]
    behaviour_logits = batch_tensors[BEHAVIOUR_LOGITS]
    unpacked_behaviour_logits = tf.split(
        behaviour_logits, output_hidden_shape, axis=1)
    unpacked_outputs = tf.split(policy.model_out, output_hidden_shape, axis=1)
    action_dist = policy.action_dist
    values = policy.value_function

    if policy.state_in:
        max_seq_len = tf.reduce_max(policy.seq_lens) - 1
        mask = tf.sequence_mask(policy.seq_lens, max_seq_len)
        mask = tf.reshape(mask, [-1])
    else:
        mask = tf.ones_like(rewards)

    # Prepare actions for loss
    loss_actions = actions if is_multidiscrete else tf.expand_dims(
        actions, axis=1)

    # Inputs are reshaped from [B * T] => [T - 1, B] for V-trace calc.
    policy.loss = VTraceLoss(
        actions=make_time_major(loss_actions, drop_last=True),
        actions_logp=make_time_major(
            action_dist.logp(actions), drop_last=True),
        actions_entropy=make_time_major(
            action_dist.multi_entropy(), drop_last=True),
        dones=make_time_major(dones, drop_last=True),
        behaviour_logits=make_time_major(
            unpacked_behaviour_logits, drop_last=True),
        target_logits=make_time_major(unpacked_outputs, drop_last=True),
        discount=policy.config["gamma"],
        rewards=make_time_major(rewards, drop_last=True),
        values=make_time_major(values, drop_last=True),
        bootstrap_value=make_time_major(values)[-1],
        dist_class=Categorical if is_multidiscrete else policy.dist_class,
        valid_mask=make_time_major(mask, drop_last=True),
        vf_loss_coeff=policy.config["vf_loss_coeff"],
        entropy_coeff=policy.entropy_coeff,
        clip_rho_threshold=policy.config["vtrace_clip_rho_threshold"],
        clip_pg_rho_threshold=policy.config["vtrace_clip_pg_rho_threshold"])

    return policy.loss.total_loss


def stats(policy, batch_tensors):
    values_batched = _make_time_major(
        policy, policy.value_function, drop_last=policy.config["vtrace"])

    return {
        "cur_lr": tf.cast(policy.cur_lr, tf.float64),
        "policy_loss": policy.loss.pi_loss,
        "entropy": policy.loss.entropy,
        "entropy_coeff": tf.cast(policy.entropy_coeff, tf.float64),
        "var_gnorm": tf.global_norm(policy.var_list),
        "vf_loss": policy.loss.vf_loss,
        "vf_explained_var": explained_variance(
            tf.reshape(policy.loss.value_targets, [-1]),
            tf.reshape(values_batched, [-1])),
    }


def grad_stats(policy, grads):
    return {
        "grad_gnorm": tf.global_norm(grads),
    }


def postprocess_trajectory(policy,
                           sample_batch,
                           other_agent_batches=None,
                           episode=None):
    # not used, so save some bandwidth
    del sample_batch.data[SampleBatch.NEXT_OBS]
    return sample_batch


def add_behaviour_logits(policy):
    return {BEHAVIOUR_LOGITS: policy.model_out}


def validate_config(policy, obs_space, action_space, config):
    assert config["batch_mode"] == "truncate_episodes", \
        "Must use `truncate_episodes` batch mode with V-trace."


def choose_optimizer(policy, config):
    if policy.config["opt_type"] == "adam":
        return tf.train.AdamOptimizer(policy.cur_lr)
    else:
        return tf.train.RMSPropOptimizer(policy.cur_lr, config["decay"],
                                         config["momentum"], config["epsilon"])


def clip_gradients(policy, optimizer, loss):
    grads = tf.gradients(loss, policy.var_list)
    policy.grads, _ = tf.clip_by_global_norm(grads, policy.config["grad_clip"])
    clipped_grads = list(zip(policy.grads, policy.var_list))
    return clipped_grads


class ValueNetworkMixin(object):
    def __init__(self):
        self.value_function = self.model.value_function()
        self.var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                          tf.get_variable_scope().name)

    def value(self, ob, *args):
        feed_dict = {
            self.get_placeholder(SampleBatch.CUR_OBS): [ob],
            self.seq_lens: [1]
        }
        assert len(args) == len(self.state_in), \
            (args, self.state_in)
        for k, v in zip(self.state_in, args):
            feed_dict[k] = v
        vf = self.get_session().run(self.value_function, feed_dict)
        return vf[0]


def setup_mixins(policy, obs_space, action_space, config):
    LearningRateSchedule.__init__(policy, config["lr"], config["lr_schedule"])
    EntropyCoeffSchedule.__init__(policy, config["entropy_coeff"],
                                  config["entropy_coeff_schedule"])
    ValueNetworkMixin.__init__(policy)


VTraceTFPolicy = build_tf_policy(
    name="VTraceTFPolicy",
    get_default_config=lambda: ray.rllib.agents.impala.impala.DEFAULT_CONFIG,
    loss_fn=build_vtrace_loss,
    stats_fn=stats,
    grad_stats_fn=grad_stats,
    postprocess_fn=postprocess_trajectory,
    optimizer_fn=choose_optimizer,
    gradients_fn=clip_gradients,
    extra_action_fetches_fn=add_behaviour_logits,
    before_init=validate_config,
    before_loss_init=setup_mixins,
    mixins=[LearningRateSchedule, EntropyCoeffSchedule, ValueNetworkMixin],
    get_batch_divisibility_req=lambda p: p.config["sample_batch_size"])
