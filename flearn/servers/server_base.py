import torch
import os
import h5py
import numpy as np
import copy
from scipy.stats import rayleigh
from scipy import optimize


class Server:
    def __init__(self, dataset, algorithm, model, nb_users, nb_samples, user_ratio, sample_ratio, L, max_norm,
                 num_glob_iters, local_updates, users_per_round, similarity, noise, times, dp, epsilon_target, number):

        self.number = str(number)

        # Set up the main attributes
        self.dataset = dataset
        self.num_glob_iters = num_glob_iters
        self.local_updates = local_updates
        self.max_norm = max_norm
        self.user_ratio = user_ratio
        self.sample_ratio = sample_ratio
        self.global_learning_rate = 1.0
        self.total_train_samples = 0
        self.model = copy.deepcopy(model)
        self.dim_model = sum([torch.flatten(p.data).size().numel() for p in self.model.parameters()])
        self.users = []
        self.selected_users = []
        self.users_per_round = users_per_round
        self.nb_samples = nb_samples
        self.L = L
        self.algorithm = algorithm
        self.rs_train_acc, self.rs_train_loss, self.rs_test_loss, self.rs_glob_acc, self.rs_train_diss = [], [], [], [], []

        self.dp = dp
        self.epsilon_target = epsilon_target
        self.delta_target = 1 / (nb_users * nb_samples)

        # T is the total number of communication rounds (>= num_glob_iters used for plot)
        if dataset == "Logistic" and local_updates == 50:
            self.T = 1500
        elif dataset == "Logistic" and local_updates == 100:
            self.T = 800
        elif dataset == "Logistic" and local_updates == 200:
            self.T = 400
        elif dataset == "Femnist" and local_updates == 50:
            self.T = 800
        elif dataset == "Femnist" and local_updates == 100:
            self.T = 400
        else:
            self.T = 400

        self.sigma_g = 4 * self.user_ratio * self.sample_ratio * np.sqrt(
            self.local_updates * self.T * np.log(2 * self.T * self.user_ratio / self.delta_target) * np.log(
                2 / self.delta_target)) / self.epsilon_target

        self.times = times
        self.similarity = similarity
        self.noise = noise
        self.communication_thresh = None
        self.param_norms = []
        self.control_norms = None

    def send_parameters(self):
        assert (self.users is not None and len(self.users) > 0)
        for user in self.users:
            user.set_parameters(self.model)

    def save_model(self):
        model_path = os.path.join("models", self.dataset)
        if not os.path.exists(model_path):
            os.makedirs(model_path)
        torch.save(self.model, os.path.join(model_path, "server_" + str(self.similarity) + ".pt"))

    def load_model(self):
        model_path = os.path.join("models", self.dataset, "server_" + str(self.similarity) + ".pt")
        assert (os.path.exists(model_path))
        self.model = torch.load(model_path)

    def model_exists(self):
        return os.path.exists(os.path.join("models", self.dataset, "server_" + str(self.similarity) + ".pt"))

    def select_users(self, round, users_per_round):
        if users_per_round in [len(self.users), 0]:
            return self.users

        users_per_round = min(users_per_round, len(self.users))
        # fix the list of user consistent
        np.random.seed(round * (self.times + 1))
        return np.random.choice(self.users, users_per_round, replace=False)  # , p=pk)

    def select_transmitting_users(self):
        transmitting_users = []
        for user in self.users:
            user.csi = rayleigh.rvs()
            if user.csi >= self.communication_thresh:
                transmitting_users.append(user)
        return transmitting_users

    def save_results(self):
        """ Save metrics (except norms) to h5 file"""
        file_name = "./results/" + self.dataset + "_" + self.number + '_' + self.algorithm
        file_name += "_" + str(self.similarity) + "s"
        file_name += "_" + str(int(self.local_updates * self.sample_ratio)) + "K"
        if self.dp != "None":
            file_name += "_" + str(self.epsilon_target) + self.dp
        if self.noise:
            file_name += '_noisy'
        file_name += "_" + str(self.times) + ".h5"
        if len(self.rs_glob_acc) != 0 & len(self.rs_train_acc) & len(self.rs_train_loss) & len(self.rs_test_loss) & len(
                self.rs_train_diss):
            with h5py.File(file_name, 'w') as hf:
                hf.create_dataset('rs_glob_acc', data=self.rs_glob_acc)
                hf.create_dataset('rs_train_acc', data=self.rs_train_acc)
                hf.create_dataset('rs_train_loss', data=self.rs_train_loss)
                hf.create_dataset('rs_test_loss', data=self.rs_test_loss)
                hf.create_dataset('rs_train_diss', data=self.rs_train_diss)

    def save_norms(self):
        """ Save norms to h5 file"""
        file_name = "./results/" + self.dataset + "_" + self.number + '_' + self.algorithm + '_norms'
        file_name += "_" + str(self.similarity) + "s"
        file_name += "_" + str(int(self.local_updates / self.sample_ratio)) + "K"
        if self.dp != "None":
            file_name += "_" + str(self.epsilon_target) + self.dp
        if self.noise:
            file_name += '_noisy'
        file_name += "_" + str(self.times) + ".h5"

        if len(self.param_norms):
            with h5py.File(file_name, 'w') as hf:
                hf.create_dataset('rs_param_norms', data=self.param_norms)
                if self.algorithm == 'SCAFFOLD' or self.algorithm == 'SCAFFOLD-warm':
                    hf.create_dataset('rs_control_norms', data=self.control_norms)

    def test_error_and_loss(self):
        """Excess error of the current model of all users (test data)"""
        num_samples = []
        tot_correct = []
        losses = []
        for c in self.users:
            ct, cl, ns = c.test_error_and_loss()
            tot_correct.append(ct * 1.0)
            num_samples.append(ns)
            losses.append(cl * 1.0)
        ids = [c.user_id for c in self.users]

        return ids, num_samples, tot_correct, losses

    def train_error_and_loss(self):
        """Excess error of the current model of all users (train data)"""
        num_samples = []
        tot_correct = []
        losses = []
        losses_diff = []
        for c in self.users:
            ct, cl, cl_lowest, ns = c.train_error_and_loss()
            tot_correct.append(ct * 1.0)
            num_samples.append(ns)
            losses.append(cl * 1.0)
            losses_diff.append((cl - cl_lowest) * 1.0)

        ids = [c.user_id for c in self.users]
        # groups = [c.group for c in self.clients]

        return ids, num_samples, tot_correct, losses, losses_diff

    def train_dissimilarity(self):
        """Gradient dissimilarity of the current model of all users (train data)"""
        dissimilarities = []
        for c in self.users:
            dissimilarities.append(c.train_dissimilarity())
        return dissimilarities

    def evaluate(self):
        stats_test = self.test_error_and_loss()
        stats_train = self.train_error_and_loss()
        dissimilarity = self.train_dissimilarity()

        train_diss_1 = sum([torch.norm(dis).numpy() ** 2 for dis in dissimilarity]) / len(self.users)
        train_diss_2 = torch.norm(
            sum([dis for dis in dissimilarity]) / len(self.users)).numpy() ** 2

        train_diss = train_diss_1 - train_diss_2

        glob_acc = np.sum(stats_test[2]) * 1.0 / np.sum(stats_test[1])
        train_acc = np.sum(stats_train[2]) * 1.0 / np.sum(stats_train[1])

        train_loss_diff = sum([x.item() for x in stats_train[4]]) / len(self.users)
        train_loss = sum([x.item() for x in stats_train[3]]) / len(self.users)

        test_loss = sum([x.item() for x in stats_test[3]]) / len(self.users)

        self.rs_glob_acc.append(glob_acc)
        self.rs_test_loss.append(test_loss)
        self.rs_train_acc.append(train_acc)
        if self.dp == "None" and (self.similarity == "iid" or self.similarity == 1.0):
            self.rs_train_loss.append(train_loss)
        else:
            self.rs_train_loss.append(train_loss_diff)
        self.rs_train_diss.append(train_diss)

        print("Similarity:", self.similarity)
        print("Average Global Test Accuracy: ", round(glob_acc, 5))
        print("Average Global Test Loss: ", round(test_loss, 5))
        print("Average Global Training Accuracy: ", round(train_acc, 5))
        if self.dp != "None" or not (self.similarity == "iid" or self.similarity == 1.0):
            print("Average Global F(x_t)-F(x*): ", round(train_loss_diff, 5))
        print("Average Global Training Loss: ", round(train_loss, 5))
        print("Average Global Training Gradient Dissimilarity: ", round(train_diss, 5))
        print("Average Global Training Gradient Dissimilarity (mean of norms): ", round(train_diss_1, 5))
        print("Average Global Training Gradient Dissimilarity (norm of mean): ", round(train_diss_2, 5))