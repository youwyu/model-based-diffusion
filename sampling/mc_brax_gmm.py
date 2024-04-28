import functools
import os
from datetime import datetime
from brax import envs
from brax.training.agents.ppo import train as ppo
from brax.training.agents.ppo import networks as ppo_networks
from brax.training.acme import running_statistics
from brax.io import model, html
import jax
from jax import lax
from jax import numpy as jnp
from matplotlib import pyplot as plt
from jax import config

# config.update("jax_enable_x64", True) # NOTE: this is important for simulating long horizon open loop control

## global config

use_data = False
init_data = False

## setup env

env_name = "point"
backend = "positional"
if env_name in ["hopper", "walker2d"]:
    substeps = 10
elif env_name in ["humanoid", "humanoidstandup"]:
    substeps = 2
else:
    substeps = 1
if env_name == "pushT":
    from pushT import PushT

    env = PushT()
elif env_name == "point":
    from point import Point, vis_env

    env = Point()
else:
    env = envs.get_environment(env_name=env_name, backend=backend)
Nx = env.observation_size
Nu = env.action_size
step_env_jit = jax.jit(env.step)

if substeps > 1:

    @jax.jit
    def step_env(state, u):
        def step_once(state, unused):
            return step_env_jit(state, u), state.reward

        state, rews = lax.scan(step_once, state, None, length=substeps)
        state = state.replace(reward=rews.mean())
        return state

else:
    step_env = step_env_jit

reset_env = jax.jit(env.reset)
rng = jax.random.PRNGKey(seed=0)
rng, rng_reset = jax.random.split(rng)  # NOTE: rng_reset should never be changed.
state_init = reset_env(rng_reset)
path = f"../figure/{env_name}/{backend}"
if not os.path.exists(path):
    os.makedirs(path)

## run diffusion

Nexp = 16
Nsample = 1024
Hsample = 50
if env_name == "point":
    Nsample = 4096
    Hsample = 20
Ndiffuse = 100
temp_sample = 0.5
beta0 = 1e-4
betaT = 1e-2
betas = jnp.linspace(beta0, betaT, Ndiffuse)
alphas = 1.0 - betas
alphas_bar = jnp.cumprod(alphas)
sigmas = jnp.sqrt(1 - alphas_bar)
Sigmas_cond = (1 - alphas) * (1 - jnp.sqrt(jnp.roll(alphas_bar, 1))) / (1 - alphas_bar)
sigmas_cond = jnp.sqrt(Sigmas_cond)
sigmas_cond = sigmas_cond.at[0].set(0.0)
print(f"init sigma = {sigmas[-1]:.2e}")

rng, rng_y0 = jax.random.split(rng)
Y0s_hat = jax.random.normal(rng_y0, (Nsample, Hsample, Nu))
logs_Y0_hat = jnp.zeros(Nsample)
rng, rng_y = jax.random.split(rng)
Yts = jax.random.normal(rng_y, (Nexp, Hsample, Nu))


def sample_GMM(means, log_weights, key, sigma, num_samples):
    components = jax.random.categorical(key, log_weights, shape=(num_samples,))
    samples = means[components] + sigma * jax.random.normal(key, (num_samples,))
    return samples


sample_GMM_vmap = jax.vmap(sample_GMM, in_axes=(1, None, 0, None, None))
sample_GMM_vvmap = jax.vmap(sample_GMM_vmap, in_axes=(1, None, 0, None, None))


def get_logp_GMM(means, log_weights, x, sigma):
    log_probs = jnp.log(jax.nn.softmax(log_weights))
    log_probs = log_probs + jax.scipy.stats.norm.logpdf(x, means, sigma)
    return jax.scipy.special.logsumexp(log_probs)


get_logp_GMM_vmap = jax.vmap(get_logp_GMM, in_axes=(1, None, 0, None))
get_logp_GMM_vvmap = jax.vmap(get_logp_GMM_vmap, in_axes=(1, None, 0, None))
get_logp_GMM_vvvmap = jax.vmap(get_logp_GMM_vvmap, in_axes=(None, None, 0, None))


# evaluate the diffused uss
@jax.jit
def eval_us(state, us):
    def step(state, u):
        state = step_env(state, u)
        return state, state.reward

    _, rews = jax.lax.scan(step, state, us)
    return rews


@jax.jit
def rollout_us(state, us):
    def step(state, u):
        state = step_env(state, u)
        return state, state.obs

    _, rollout = jax.lax.scan(step, state, us)
    return rollout


def render_us(state, us, name="rollout"):
    rollout = []
    rew_sum = 0.0
    for i in range(Hsample):
        for j in range(substeps):
            rollout.append(state.pipeline_state)
            state = step_env_jit(state, us[i])
            rew_sum += state.reward
    rew_mean = rew_sum / (Hsample * substeps)
    webpage = html.render(env.sys.replace(dt=env.dt), rollout)
    print(f"evaluated reward mean: {rew_mean:.2e}")
    with open(f"{path}/{name}.html", "w") as f:
        f.write(webpage)


@jax.jit
def reverse_once(carry, unused):
    # mean_Y0s: (Nsample, Hsample, Nu)
    # logq_Y0s: (Nsample)
    t, rng, mean_Y0s, logq_Y0s, Yts = carry

    # sample Y0s
    rng, rng_Y0s = jax.random.split(rng)
    rng_Y0s = jax.random.split(rng_Y0s, (Hsample, Nu))
    sigma_Y0 = sigmas[t] / jnp.sqrt(Nsample)
    Y0s = sample_GMM_vvmap(mean_Y0s, logq_Y0s, rng_Y0s, sigma_Y0, Nsample) # (Hsample, Nu, Nsample)
    Y0s = jnp.moveaxis(Y0s, 2, 0) # (Nsample, Hsample, Nu)
    logq_Y0s = get_logp_GMM_vvvmap(mean_Y0s, logq_Y0s, Y0s, sigma_Y0).mean(axis=[1,2]) # (Nsample)

    # calculate logp_Y0s_new
    eps_Yt = jnp.clip(
        (Y0s[None] * jnp.sqrt(alphas_bar[t]) - Yts[:, None]) / sigmas[t], -2.0, 2.0
    )  # (Nexp, Nsample, Hsample, Nu)
    logp_Yt_Y0s = -0.5 * jnp.mean(eps_Yt**2, axis=(2, 3))  # (Nexp, Nsample)
    rews = jax.vmap(eval_us, in_axes=(None, 0))(state_init, Y0s).mean(axis=-1)
    rews_normed = (rews - rews.mean()) / rews.std()  # p(Y0)
    logp_Y0s = rews_normed / temp_sample
    logp_Y0s_bar = logp_Y0s + logp_Yt_Y0s - logq_Y0s  # p(Y0) * p(Yt|Y0) / q(Y0)
    logp_Y0s_bar = logp_Y0s_bar - jnp.max(logp_Y0s_bar)
    logq_Y0s_new = logp_Y0s - logq_Y0s
    logq_Y0s_new = logq_Y0s_new - jnp.max(logq_Y0s_new)

    # calculate mean_Y0s_new
    Y0s_bar = jnp.einsum(
        "mn,nij->mij", jax.nn.softmax(logp_Y0s_bar, axis=-1), Y0s
    )  # (Nexp, Hsample, Nu)
    mean_Y0s_new = Y0s

    # calculate Ytm1
    scores = (
        alphas_bar[t] / (1 - alphas_bar[t]) * (Y0s_bar - Yts / jnp.sqrt(alphas_bar[t]))
    )
    rng, Ytm1_rng = jax.random.split(rng)
    eps_Ytm1 = jax.random.normal(Ytm1_rng, (Nexp, Hsample, Nu))
    Ytsm1 = (
        1 / jnp.sqrt(1.0 - betas[t]) * (Yts + 0.5 * betas[t] * scores)
        + 1.0 * jnp.sqrt(betas[t]) * eps_Ytm1
    )

    jax.debug.print("=============t={x}============", x=t)
    # jax.debug.print("std_w_rew={x}", x=std_w_rew)
    jax.debug.print("rews={x} \pm {y}", x=rews.mean(), y=rews.std())
    jax.debug.print(
        "Y0 hat best = {x}",
        x=jax.vmap(eval_us, in_axes=(None, 0))(state_init, mean_Y0s)
        .mean(axis=-1)
        .max(),
    )
    jax.debug.print(
        "Yt best = {x}",
        x=jax.vmap(eval_us, in_axes=(None, 0))(
            state_init, Yts / jnp.sqrt(alphas_bar[t])
        )
        .mean(axis=-1)
        .max(),
    )

    return (t - 1, rng, mean_Y0s_new, logq_Y0s_new, Ytsm1), rews.mean()


# run reverse
def reverse(Y0s_hat, logs_Y0_hat, Yts, rng):
    carry_once = (Ndiffuse - 1, rng, Y0s_hat, logs_Y0_hat, Yts)
    # (t0, rng, Y0s_hat, weights_Y0_hat, Y0s), rew = jax.lax.scan(
    #     reverse_once, carry_once, None, Ndiffuse
    # )
    fig, ax = plt.subplots()

    for i in range(Ndiffuse):
        carry_once, rew = reverse_once(carry_once, None)
        if env_name == "point":
            Y0s_hat = carry_once[2]  # (Nsample, Hsample, Nu)
            X0s_hat = jax.vmap(rollout_us, in_axes=(None, 0))(state_init, Y0s_hat)
            vis_env(ax, X0s_hat[:16])
            # plt.savefig(f"{path}/{i}.png")
            # plt.savefig("../figure/point.png")
            plt.pause(0.1)

    (tT, rng, Y0_hat, Y0), rew = carry_once
    rng, rng_Y0 = jax.random.split(rng)
    idx = jax.random.categorical(rng_Y0, logs_Y0_hat, shape=(Nsample,))
    Y0s_hat = Y0s_hat[idx]
    return Y0s_hat, Y0s, rew


rng, rng_exp = jax.random.split(rng)
Y0s_hat, Y0s, rew_exp = reverse(Y0s_hat, logs_Y0_hat, Yts, rng_exp)
rew_eval = jax.vmap(eval_us, in_axes=(None, 0))(state_init, Y0s_hat).mean(axis=-1)
print(f"rews mean: {rew_eval.mean():.2e} std: {rew_eval.std():.2e}")

for j in range(8):
    render_us(state_init, Y0s_hat[j], f"rollout{j}")
