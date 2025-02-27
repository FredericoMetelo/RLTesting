import numpy as np
from peersim_gym.envs.PeersimEnv import PeersimEnv
from torchsummary import summary

import torch as T
from src.MARL.Agent import Agent
from src.MARL.Networks.A2C import ActorCritic
from src.Utils import utils

import peersim_gym.envs.PeersimEnv as pe
from src.Utils.MetricHelper import MetricHelper as mh
import peersim_gym.envs.PeersimEnv as pg
from tqdm import tqdm


class A2CAgentMARL(Agent):
    """
    Actor-Critic Agent
    Sources:
    - https://www.youtube.com/watch?v=OcIx_TBu90Q&t=1050s  | Video with tips of how to implement A3C
    """

    def __init__(self, input_shape, action_space, output_shape, agents, learning_rate=0.7, gamma=0.4,
                 steps_for_return=150,
                 collect_data=False, save_interval=50, control_type="A2C"):
        super().__init__(input_shape, action_space, output_shape, learning_rate, collect_data=collect_data)
        self.steps_for_return = steps_for_return
        self.gamma = gamma
        self.control_type = control_type
        self.possible_agents = agents
        self.save_interval = save_interval

        self.A2Cs = {}
        self.action_shape = output_shape
        for agent in self.possible_agents:
            rank = output_shape[agent]
            self.A2Cs[agent] = ActorCritic(lr=learning_rate, input_dims=self.input_shape, fc1_dims=512, fc2_dims=256,
                                           fc3_dims=128,
                                           n_actions=rank)
            summary(self.A2Cs[agent], input_size=self.input_shape)

        self.amount_of_metrics = 50
        self.last_losses = np.zeros(self.amount_of_metrics)
        self.last_rewards = np.zeros(self.amount_of_metrics)

        self.agent_states = {
            agent: {
                'state': [],
                'action': [],
                'reward': [],
                'next_state': [],
                'done': []
            } for agent in agents
        }

    def train_loop(self, env: PeersimEnv, num_episodes, print_instead=True, controllers=None, warm_up_file=None,
                   load_weights=None,
                   results_file=None):
        super().train_loop(env, num_episodes, print_instead, controllers)
        # See page 14 from: https://arxiv.org/pdf/1602.01783v2.pdf

        scores, episodes, avg_scores, obj, avg_episode = [], [], [], [], []
        steps_per_return = self.steps_for_return
        self.mh = mh(agents=env.possible_agents, num_nodes=env.number_nodes, num_episodes=num_episodes,
                     file_name=results_file + "_result")

        if load_weights is not None:
            for idx, agent in enumerate(env.possible_agents):
                agent_w = load_weights + f"_{agent}.pth.tar"
                self.A2Cs[agent].load_checkpoint(agent_w)

        for i in tqdm(range(num_episodes)):
            # Prepare variables for the next run
            dones = [False for _ in controllers]
            agent_list = env.agents
            step = 0
            # Episode metrics
            score = 0.0

            # Reset the state
            states, _ = env.reset()
            states = utils.flatten_state_list(states, agent_list)

            while not utils.is_done(dones):
                print(f'Step: {step}\n')
                # Interaction Step:
                targets = {agent: np.floor(self.get_action(np.array([states[idx]])[0], agent)) for idx, agent in
                           enumerate(agent_list)}
                actions = utils.make_action(targets, agent_list)

                self.mh.register_actions(actions)

                next_states, rewards, dones, _, info = env.step(actions)
                next_states = utils.flatten_state_list(states=next_states, agents=agent_list)

                for idx, agent in enumerate(agent_list):
                    total_reward_in_step = self.__store_agent_step_data(states, actions, rewards, next_states, dones,
                                                                        agent_list)
                    score += total_reward_in_step

                # Advance to next iter
                states = next_states
                last_losses = {agent: 0 for agent in agent_list}

                step += 1
                if step % steps_per_return == 0 or self.check_all_done(dones):
                    # Here we will learn the paths from all the agents
                    print("Training...")
                    for agent in agent_list:
                        s, a, r, s_next, fin = self.__get_agent_step_data(agent)
                        if s and a and r and s_next and fin:  # Check if fin is always not empty as well
                            last_loss = self.learn(s=s, a=a, r=r, s_next=s_next, k=step, fin=fin, agent=agent)
                            last_losses[agent] = last_loss if not last_loss is None else 0
                    self.__clean_agent_step_data(agent_list)

                print(f'Action{actions}  -   Loss: {last_losses}  -    Rewards: {rewards}')
                self.mh.update_metrics_after_step(rewards=rewards,
                                                  losses=last_losses,
                                                  overloaded_nodes=info[pg.STATE_G_OVERLOADED_NODES],
                                                  average_response_time=info[pg.STATE_G_AVERAGE_COMPLETION_TIMES],
                                                  occupancy=info[pg.STATE_G_OCCUPANCY],
                                                  dropped_tasks=info[pg.STATE_G_DROPPED_TASKS],
                                                  finished_tasks=info[pg.STATE_G_FINISHED_TASKS],
                                                  total_tasks=info[pg.STATE_G_TOTAL_TASKS],
                                                  consumed_energy=info[pg.STATE_G_CONSUMED_ENERGY])

            # Update final metrics
            self.mh.print_action_density_episode()
            self.mh.compile_aggregate_metrics(i, step)
            if i % self.save_interval == 0:
                for agent in env.agents:
                    self.A2Cs[agent].save_checkpoint(filename=f"{self.control_type}_value_{i}_{agent}.pth.tar", epoch=i)

            print("Episode {0}/{1}, Score: {2}, AVG Score: {3}".format(i, num_episodes, score,
                                                                       self.mh.episode_average_reward(i)))

        if results_file is not None:
            self.mh.store_as_cvs(results_file)
        self.mh.plot_agent_metrics(num_episodes=num_episodes, title=self.control_type, print_instead=print_instead)
        self.mh.plot_simulation_data(num_episodes=num_episodes, title=self.control_type, print_instead=print_instead)
        self.mh.clean_plt_resources()

    def learn(self, s, a, r, s_next, k, fin, agent):
        self.A2Cs[agent].remember_batch(states=s, actions=a, rewards=r, next_states=s_next, dones=fin)  # States should be ordered.
        self.A2Cs[agent].optimizer.zero_grad()
        loss = self.A2Cs[agent].calculate_loss(fin)
        loss.backward()
        T.nn.utils.clip_grad_value_(self.A2Cs[agent].parameters(), 10)
        self.A2Cs[agent].optimizer.step()
        self.A2Cs[agent].clear_memory()
        return loss.item()

    def get_action(self, observation, agent):
        self.A2Cs[agent].eval()
        with T.no_grad():
            action = self.A2Cs[agent].choose_action(observation)
        self.A2Cs[agent].train()
        return action

    def __store_agent_step_data(self, states, actions, rewards, next_states, dones, agent_list):
        total_rwrd = 0
        for idx, agent in enumerate(agent_list):
            # Update history
            agent_data = self.agent_states[agent]
            agent_data['state'].append(states[idx])
            agent_data['action'].append(actions[agent][pe.ACTION_NEIGHBOUR_IDX_FIELD])
            agent_data['reward'].append(rewards[agent])
            agent_data['next_state'].append(next_states[idx])
            agent_data['done'].append(dones[agent])
            total_rwrd += rewards[agent]
        return total_rwrd

    def __get_agent_step_data(self, agent):
        agent_data = self.agent_states[agent]
        return agent_data['state'], agent_data['action'], agent_data['reward'], agent_data['next_state'], agent_data[
            'done']

    def __clean_agent_step_data(self, agents):
        for agent in agents:
            self.agent_states[agent] = {
                'state': [],
                'action': [],
                'reward': [],
                'next_state': [],
                'done': []
            }

    def tally_actions(self, actions):
        for worker, action in actions.items():
            self.mh.register_action(action[pe.ACTION_NEIGHBOUR_IDX_FIELD], worker)

    def check_all_done(self, dones):
        """
        This is possible because all the agents are done at the same time, and only when the episode ends
        :param dones:
        :return:
        """
        return any([d for d in dones.values()])
