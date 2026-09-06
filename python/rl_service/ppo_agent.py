import torch
from torch.distributions import Categorical

from action_codec import DiscreteActionCodec
from networks import ActorCriticDiscrete


class ACAgent:
    """Shared Actor-Critic agent for PPO and A2C inference."""

    def __init__(self, obs_dim, action_dim, device="cpu"):
        self.device = torch.device(device)
        self.model = ActorCriticDiscrete(obs_dim, action_dim).to(self.device)
        self.model.eval()
        self.codec = DiscreteActionCodec()

    def act(self, obs, deterministic=True):
        with torch.no_grad():
            x = torch.tensor(obs, dtype=torch.float32, device=self.device).unsqueeze(0)
            logits, value = self.model(x)
            dist = Categorical(logits=logits)
            action_idx = torch.argmax(logits, dim=-1) if deterministic else dist.sample()
            logp = dist.log_prob(action_idx)
        action = self.codec.decode(int(action_idx.item()))
        return action, int(action_idx.item()), float(logp.item()), float(value.item())

    def save(self, path):
        torch.save(self.model.state_dict(), path)

    def load(self, path):
        self.model.load_state_dict(torch.load(path, map_location=self.device, weights_only=True))
        self.model.eval()


# Backward-compatible aliases
PPOAgent = ACAgent
