"""Branching Dueling Q-Network agent for factored discrete action spaces.

Decomposes the joint action (400 combinations) into 4 independent branches:
  channel (10) × power (5) × bw (4) × mode (2)

Q(s, a1..a4) = V(s) + sum_i (A_i(s, a_i) - mean_{a_i'} A_i(s, a_i'))

Reference: Tavakoli et al., "Action Branching Architectures for
Deep Reinforcement Learning", AAAI 2018.
"""
import random
from collections import deque

import numpy as np
import torch
import torch.nn.functional as F

from action_codec import DiscreteActionCodec
from networks import BranchingDuelQ


class ReplayBuffer:
    def __init__(self, capacity=100000):
        self.buf = deque(maxlen=capacity)

    def push(self, s, a_indices, r, ns, d):
        self.buf.append((s, np.array(a_indices, dtype=np.int64), r, ns, d))

    def sample(self, batch_size=64):
        batch = random.sample(self.buf, min(batch_size, len(self.buf)))
        s, a, r, ns, d = zip(*batch)
        return (
            np.array(s, dtype=np.float32),
            np.array(a, dtype=np.int64),
            np.array(r, dtype=np.float32),
            np.array(ns, dtype=np.float32),
            np.array(d, dtype=np.float32),
        )

    def __len__(self):
        return len(self.buf)


class BDQAgent:
    """Branching Dueling Q-Network agent.

    Uses epsilon-greedy exploration + double Q-learning + target network.
    """

    def __init__(self, obs_dim, device="cpu"):
        self.device = torch.device(device)
        self.action_branches = [10, 5, 4, 2]  # channel, power, bw, mode
        self.codec = DiscreteActionCodec()
        self.replay = ReplayBuffer()

        self.model = BranchingDuelQ(obs_dim, self.action_branches).to(self.device)
        self.target = BranchingDuelQ(obs_dim, self.action_branches).to(self.device)
        self.target.load_state_dict(self.model.state_dict())
        self.model.eval()
        self.target.eval()

        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=1e-3)
        self.epsilon = 1.0
        self.eps_min = 0.05

    def _branch_indices(self, action_dict):
        """Convert action dict to branch indices [ch_idx, p_idx, bw_idx, mode_idx]."""
        ch = int(action_dict["channel_idx"])
        p = self.codec.power_levels_db.index(int(action_dict["power_db"])) if int(action_dict["power_db"]) in self.codec.power_levels_db else 0
        bw = self.codec.bw_choices_mhz.index(int(action_dict["bw_mhz"])) if int(action_dict["bw_mhz"]) in self.codec.bw_choices_mhz else 0
        m = int(action_dict["waveform_mode"])
        return [ch, p, bw, m]

    def act(self, obs, deterministic=True):
        with torch.no_grad():
            x = torch.tensor(obs, dtype=torch.float32, device=self.device).unsqueeze(0)
            qs, v = self.model(x)
            if deterministic:
                indices = [q.argmax(dim=-1).item() for q in qs]
            else:
                indices = [q.argmax(dim=-1).item() if random.random() > self.epsilon
                          else random.randrange(q.shape[-1])
                          for q in qs]
            value = v.item()

        action = self.codec.decode_from_branches(*indices)
        return action, value

    def update(self, batch_size=64, gamma=0.99):
        if len(self.replay) < batch_size:
            return None

        s, a_indices, r, ns, d = self.replay.sample(batch_size)
        s = torch.tensor(s, dtype=torch.float32, device=self.device)
        a_indices = torch.tensor(a_indices, dtype=torch.long, device=self.device)  # [B, 4]
        r = torch.tensor(r, dtype=torch.float32, device=self.device)
        ns = torch.tensor(ns, dtype=torch.float32, device=self.device)
        d = torch.tensor(d, dtype=torch.float32, device=self.device)

        with torch.no_grad():
            target_qs, _ = self.target(ns)
            online_qs, _ = self.model(ns)
            # Double Q: greedy action from online, value from target
            target_vals = []
            for i, (tq, oq) in enumerate(zip(target_qs, online_qs)):
                best_a = oq.argmax(dim=-1)  # [B]
                target_vals.append(tq.gather(1, best_a.unsqueeze(-1)).squeeze(-1))
            target_q = sum(target_vals)  # [B]
            y = r + gamma * (1.0 - d) * target_q

        qs, _ = self.model(s)
        q = sum(q.gather(1, a_indices[:, i].unsqueeze(-1)).squeeze(-1)
                for i, q in enumerate(qs))
        loss = F.mse_loss(q, y)

        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), 5.0)
        self.optimizer.step()

        return float(loss.item())

    def sync_target(self, tau=0.005):
        for p, tp in zip(self.model.parameters(), self.target.parameters()):
            tp.data.copy_(tau * p.data + (1.0 - tau) * tp.data)

    def decay_epsilon(self, step, total_steps):
        self.epsilon = max(self.eps_min, 1.0 - (1.0 - self.eps_min) * step / total_steps)

    def save(self, path):
        torch.save(self.model.state_dict(), path)

    def load(self, path):
        self.model.load_state_dict(torch.load(path, map_location=self.device, weights_only=True))
        self.target.load_state_dict(self.model.state_dict())
        self.model.eval()
        self.target.eval()
