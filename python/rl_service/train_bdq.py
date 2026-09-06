import argparse

import numpy as np

from jammer_ctrl_env import JammerSpectrumCtrlEnv
from bdq_agent import BDQAgent


def train(args):
    env = JammerSpectrumCtrlEnv()
    agent = BDQAgent(env.obs_dim, device=args.device)
    obs = env.reset()

    ep_rewards = []
    ep_rew = 0.0
    best_mean_reward = -1e9

    for step in range(args.total_steps):
        agent.decay_epsilon(step, args.total_steps)

        action, value = agent.act(obs, deterministic=False)
        branch_indices = agent._branch_indices(action)
        next_obs, rew, done, _ = env.step_action(action)

        agent.replay.push(obs, branch_indices, rew, next_obs, float(done))

        ep_rew += rew
        obs = next_obs
        if done:
            ep_rewards.append(ep_rew)
            ep_rew = 0.0
            obs = env.reset()

        if len(agent.replay) >= args.batch_size:
            loss = agent.update(batch_size=args.batch_size, gamma=args.gamma)
            if step % args.target_update == 0:
                agent.sync_target(tau=args.tau)

        if (step + 1) % args.log_interval == 0:
            mean_rew = np.mean(ep_rewards[-20:]) if ep_rewards else 0.0
            eps = agent.epsilon
            print(f"[BDQ] step {step + 1}/{args.total_steps}  "
                  f"mean_ep_rew(20)={mean_rew:.3f}  eps={eps:.3f}  "
                  f"replay={len(agent.replay)}")

            if mean_rew > best_mean_reward and len(ep_rewards) >= 20:
                best_mean_reward = mean_rew
                agent.save(args.output)
                print(f"[BDQ] new best model saved (mean_rew={best_mean_reward:.3f})")

    agent.save(args.output + ".final")
    print(f"[BDQ] final model saved to {args.output}.final")


def parse_args():
    p = argparse.ArgumentParser(description="BDQ training for spectrum jammer control")
    p.add_argument("--total-steps", type=int, default=50000)
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--gamma", type=float, default=0.99)
    p.add_argument("--tau", type=float, default=0.005)
    p.add_argument("--target-update", type=int, default=200)
    p.add_argument("--log-interval", type=int, default=2000)
    p.add_argument("--device", default="cpu")
    p.add_argument("--output", default="checkpoints/bdq_spectrum_ctrl.pt")
    return p.parse_args()


if __name__ == "__main__":
    train(parse_args())
