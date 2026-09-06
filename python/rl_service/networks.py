import torch
import torch.nn as nn


class ActorCriticDiscrete(nn.Module):
    def __init__(self, obs_dim, action_dim, hidden_dim=128):
        super().__init__()
        self.backbone = nn.Sequential(
            nn.Linear(obs_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(),
        )
        self.policy_head = nn.Linear(hidden_dim, action_dim)
        self.value_head = nn.Linear(hidden_dim, 1)

    def forward(self, x):
        z = self.backbone(x)
        return self.policy_head(z), self.value_head(z)


class BranchingDuelQ(nn.Module):
    """Branching Dueling Q-Network for factored discrete action spaces.

    Action branches: channel(10) × power(5) × bw(4) × mode(2) = 400 total.
    Q(s, a1..a4) = V(s) + sum_i (A_i(s, a_i) - mean_{a_i'} A_i(s, a_i')).
    """
    def __init__(self, obs_dim, action_branches, hidden_dim=128):
        super().__init__()
        self.action_branches = action_branches  # list of branch sizes, e.g. [10,5,4,2]
        self.backbone = nn.Sequential(
            nn.Linear(obs_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(),
        )
        self.value = nn.Linear(hidden_dim, 1)
        self.advantages = nn.ModuleList([
            nn.Linear(hidden_dim, sz) for sz in action_branches
        ])

    def forward(self, x):
        z = self.backbone(x)
        v = self.value(z)  # [B, 1]
        advs = [adv(z) for adv in self.advantages]  # list of [B, branch_sz]
        qs = []
        for i, a in enumerate(advs):
            mean_a = a.mean(dim=-1, keepdim=True)
            q = v + (a - mean_a)
            qs.append(q)
        return qs, v
