from brax import actuator
from brax import base
from brax.envs.base import PipelineEnv, State
from brax.io import mjcf, html
from etils import epath
import jax
from jax import numpy as jnp
import mujoco

import mbd


class HumanoidTrack(PipelineEnv):

    def __init__(self, backend="positional", **kwargs):
        sys = mjcf.load(f"{mbd.__path__[0]}/assets/humanoidtrack.xml")
        n_frames = 5
        kwargs["n_frames"] = kwargs.get("n_frames", n_frames)
        self.torso_idx = sys.link_names.index("torso")
        body_names = [
            'torso', 
            'left_thigh',
            'right_thigh', 
            'left_shin',
            'right_shin',
        ]
        self.track_body_names = body_names
        self.track_body_idx = {
            name: sys.link_names.index(name) for name in self.track_body_names
        }
        self.ref_body_names = [name + "_ref" for name in body_names]
        self.ref_body_idx = {
            name: sys.link_names.index(name) for name in self.ref_body_names
        }

        super().__init__(sys=sys, backend=backend, **kwargs)

    def reset(self, rng: jax.Array) -> State:
        """Resets the environment to an initial state."""

        qpos = self.sys.init_q
        qvel = jnp.zeros(self.sys.qd_size())

        pipeline_state = self.pipeline_init(qpos, qvel)
        obs = self._get_obs(pipeline_state)
        reward, done, zero = jnp.zeros(3)
        metrics = {
            "reward_linup": zero,
            "reward_quadctrl": zero,
        }
        return State(pipeline_state, obs, reward, done, metrics)

    def step(self, state: State, action: jax.Array) -> State:
        """Runs one timestep of the environment's dynamics."""
        pipeline_state = self.pipeline_step(state.pipeline_state, action)

        # quad_impact_cost is not computed here

        obs = self._get_obs(pipeline_state)
        reward = self._get_reward(pipeline_state)

        return state.replace(pipeline_state=pipeline_state, obs=obs, reward=reward)

    def _get_obs(self, pipeline_state: base.State) -> jax.Array:
        return jnp.concatenate([pipeline_state.q, pipeline_state.qd], axis=-1)

    def _get_reward(self, pipeline_state: base.State) -> jax.Array:
        return (
            # pipeline_state.x.pos[0, 0]
            - jnp.clip(jnp.abs(pipeline_state.x.pos[0, 2] - 1.2), -1.0, 1.0)
            - jnp.abs(pipeline_state.x.pos[0, 1]) * 0.1
        )


def main():
    env = HumanoidTrack()
    rng = jax.random.PRNGKey(1)
    env_step = jax.jit(env.step)
    env_reset = jax.jit(env.reset)
    state = env_reset(rng)
    rollout = [state.pipeline_state]
    for _ in range(1):
        rng, rng_act = jax.random.split(rng)
        act = jax.random.uniform(rng_act, (env.action_size,), minval=-1.0, maxval=1.0)
        state = env_step(state, act)
        print(state.pipeline_state.x.pos[0, 2])
        rollout.append(state.pipeline_state)
    webpage = html.render(env.sys.replace(dt=env.dt), rollout)
    with open("../figure/humanoid.html", "w") as f:
        f.write(webpage)


if __name__ == "__main__":
    main()