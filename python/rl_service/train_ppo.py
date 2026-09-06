import argparse

import torch
import torch.nn.functional as F
from torch.distributions import Categorical

from jammer_ctrl_env import JammerSpectrumCtrlEnv
from ppo_agent import PPOAgent


def compute_gae(rewards, values, dones, gamma=0.99, lam=0.95, bootstrap_value=0.0):
    """GAE with proper bootstrap from the value of the final next state."""
    advantages = []
    gae = 0.0
    next_value = bootstrap_value
    for t in range(len(rewards) - 1, -1, -1):
        delta = rewards[t] + gamma * next_value * (1.0 - dones[t]) - values[t]
        gae = delta + gamma * lam * (1.0 - dones[t]) * gae
        advantages.insert(0, gae)
        next_value = values[t]
    returns = [a + v for a, v in zip(advantages, values)]
    return advantages, returns


def train(args):
    env = JammerSpectrumCtrlEnv()
    agent = PPOAgent(env.obs_dim, env.action_dim, device=args.device)
    optimizer = torch.optim.Adam(agent.model.parameters(), lr=args.lr)

    obs = env.reset()
    best_mean_reward = -1e9

    for upd in range(args.updates):
        obs_buf, act_idx_buf, logp_buf, rew_buf, done_buf, val_buf = [], [], [], [], [], []
        ep_rewards = []
        ep_rew = 0.0

        for _ in range(args.rollout_steps):
            action, act_idx, logp, val = agent.act(obs, deterministic=False)
            next_obs, rew, done, _ = env.step_action(action)

            obs_buf.append(obs)
            act_idx_buf.append(act_idx)
            logp_buf.append(logp)
            rew_buf.append(rew)
            done_buf.append(float(done))
            val_buf.append(val)

            ep_rew += rew
            obs = next_obs
            if done:
                ep_rewards.append(ep_rew)
                ep_rew = 0.0
                obs = env.reset()

        # Bootstrap GAE from final state's value
        with torch.no_grad():
            next_obs_t = torch.tensor(obs, dtype=torch.float32, device=agent.device).unsqueeze(0)
            _, final_val = agent.model(next_obs_t)
            bootstrap_value = float(final_val.item())

        adv, ret = compute_gae(rew_buf, val_buf, done_buf,
                               gamma=args.gamma, lam=args.lam,
                               bootstrap_value=bootstrap_value)

        obs_t = torch.tensor(obs_buf, dtype=torch.float32, device=agent.device)
        act_t = torch.tensor(act_idx_buf, dtype=torch.long, device=agent.device)
        old_logp_t = torch.tensor(logp_buf, dtype=torch.float32, device=agent.device)
        adv_t = torch.tensor(adv, dtype=torch.float32, device=agent.device)
        ret_t = torch.tensor(ret, dtype=torch.float32, device=agent.device)
        adv_t = (adv_t - adv_t.mean()) / (adv_t.std() + 1e-8)

        for _ in range(args.epochs):
            logits, values = agent.model(obs_t)
            dist = Categorical(logits=logits)
            logp = dist.log_prob(act_t)
            entropy = dist.entropy().mean()
            ratio = torch.exp(logp - old_logp_t)
            surr1 = ratio * adv_t
            surr2 = torch.clamp(ratio, 1.0 - args.clip_eps, 1.0 + args.clip_eps) * adv_t
            policy_loss = -torch.min(surr1, surr2).mean()
            value_loss = F.mse_loss(values.squeeze(-1), ret_t)
            loss = policy_loss + 0.5 * value_loss - 0.01 * entropy

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(agent.model.parameters(), 0.5)
            optimizer.step()

        mean_rew = sum(ep_rewards) / len(ep_rewards) if ep_rewards else 0.0
        entropy_val = float(entropy.item())
        print(f"[PPO] update {upd + 1}/{args.updates}  "
              f"mean_ep_rew={mean_rew:.3f}  entropy={entropy_val:.4f}  "
              f"episodes={len(ep_rewards)}")

        if mean_rew > best_mean_reward and len(ep_rewards) >= 2:
            best_mean_reward = mean_rew
            agent.save(args.output)
            print(f"[PPO] new best model saved (mean_rew={best_mean_reward:.3f})")

    agent.save(args.output + ".final")
    print(f"[PPO] final model saved to {args.output}.final")


def parse_args():
    p = argparse.ArgumentParser(description="PPO training for spectrum jammer control")
    p.add_argument("--lr", type=float, default=3e-4)
    p.add_argument("--updates", type=int, default=200)
    p.add_argument("--rollout-steps", type=int, default=512)
    p.add_argument("--epochs", type=int, default=4)
    p.add_argument("--gamma", type=float, default=0.99)
    p.add_argument("--lam", type=float, default=0.95)
    p.add_argument("--clip-eps", type=float, default=0.2)
    p.add_argument("--device", default="cpu")
    p.add_argument("--output", default="checkpoints/ppo_spectrum_ctrl.pt")
    return p.parse_args()


if __name__ == "__main__":
    train(parse_args())
