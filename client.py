from collections import OrderedDict
import random

import numpy as np
import torch
import torch.optim as optim
from sklearn.metrics import confusion_matrix
from sklearn.metrics import classification_report
from torch import nn

from attack.label_filpping import apply_class_label_replacement
from federated_learning.model.schedulers import MinCapableStepLR
import os
import numpy
import copy

class Client:

    def __init__(self, args, client_idx, train_data_loader, test_data_loader):
        """
        :param args: experiment arguments
        :type args: Arguments
        :param client_idx: Client index
        :type client_idx: int
        :param train_data_loader: Training data loader
        :type train_data_loader: torch.utils.data.DataLoader
        :param test_data_loader: Test data loader
        :type test_data_loader: torch.utils.data.DataLoader
        """
        # Client's arguments
        self.args = args
        self.client_idx = client_idx
        # print("Is Cuda Available: ", torch.cuda.is_available())
        self.device = torch.device('cpu' if torch.cuda.is_available() else 'cpu')

        # Client's neural network
        self.set_net(self.load_default_model())
        self.loss_function = self.args.get_loss_function()()
        self.optimizer = optim.SGD(self.net.parameters(),
            lr=self.args.get_learning_rate(),
            momentum=self.args.get_momentum())
        self.scheduler = MinCapableStepLR(self.args.get_logger(), self.optimizer,
            self.args.get_scheduler_step_size(),
            self.args.get_scheduler_gamma(),
            self.args.get_min_lr())

        self.mu = args.mu

        # Client's training and test data
        self.train_data_loader = train_data_loader
        self.test_data_loader = test_data_loader

    def set_mu(self, mu):
        self.mu = mu
    def get_mu(self):
        return self.mu

    def set_net(self, net):
        """
        Set the client's NN.

        :param net: torch.nn
        """
        self.net = net
        self.net.to(self.device)

    def load_default_model(self):
        """
        Load a model from default model file.

        This is used to ensure consistent default model behavior.
        """
        model_class = self.args.get_net()
        default_model_path = os.path.join(self.args.get_default_model_folder_path(), model_class.__name__ + ".model")

        return self.load_model_from_file(default_model_path)

    def load_model_from_file(self, model_file_path):
        """
        Load a model from a file.

        :param model_file_path: string
        """
        model_class = self.args.get_net()
        model = model_class()

        if os.path.exists(model_file_path):
            try:
                model.load_state_dict(torch.load(model_file_path))
            except:
                self.args.get_logger().warning("Couldn't load model. Attempting to map CUDA tensors to CPU to solve error.")

                model.load_state_dict(torch.load(model_file_path, map_location=torch.device('cpu')))
        else:
            self.args.get_logger().warning("Could not find model: {}".format(model_file_path))

        return model

    def get_client_index(self):
        """
        Returns the client index.
        """
        return self.client_idx


    #_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_ EVALUATION -_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_

    def poison_data(self, replacement_method, poison_strength):
        """
        Poison the client's training data.

        :param poison_type: Type of poisoning to apply
        :type poison_type: string
        :param poison_intensity: Intensity of the poisoning
        :type poison_intensity: float
        """
        self.args.get_logger().info("Poisoning client #{} with type: {} and intensity: {}".format(self.client_idx, replacement_method.__name__, poison_strength))

        # Poison the training data
        self.train_data_loader = apply_class_label_replacement(self.train_data_loader[0], self.train_data_loader[1],replacement_method, poison_strength )

    #_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_ TRAINING -_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_

    def get_nn_parameters(self):
        """
        Return the NN's parameters.
        """
        return self.net.state_dict()

    def update_nn_parameters(self, new_params):
        """
        Update the NN's parameters.

        :param new_params: New weights for the neural network
        :type new_params: dict
        """
        self.net.load_state_dict(copy.deepcopy(new_params), strict=True)

    # def get_attributes_name(self):
    #     # att_name = []
    #     # print(set(self.train_data_loader[1]))
    #     # for target in self.train_data_loader[1]:
    #     #     att_name.extend([t.item() for t in target])
    #     return set(self.train_data_loader[1])

    def get_attributes_name(self):
        att_name = []
        for (data, target) in self.train_data_loader:
            att_name.extend([t.item() for t in target])
        return set(att_name)

    def diff_squared_sum(self, model2):
        """Compute the squared sum of the differences between the parameters of a neural network model and another model."""
        dss = 0

        # Get parameters of the first model
        params1 = self.get_nn_parameters()

        # Check if model2 is a PyTorch model
        if isinstance(model2, nn.Module):
            # Compute squared sum of differences for weights
            for name, param2 in model2.named_parameters():
                if name in params1:
                    param1 = params1[name]
                    dss += ((param1 - param2) ** 2).sum()
        elif isinstance(model2, OrderedDict):
            # Iterate through the OrderedDict items
            for name, param2 in model2.items():
                if name in params1:
                    param1 = params1[name]
                    dss += ((param1 - param2) ** 2).sum()
        else:
            raise ValueError("Unsupported model type. Model must be a PyTorch model or an OrderedDict.")

        return dss

    def train(self, round, epochs=5, type="fed_avg", seed=40):
        """
        :param round: Current round #
        :type round: int
        :param seed: Seed for random number generation
        :type seed: int or None
        """
        # Set random seed for reproducibility
        if seed is not None:
            torch.manual_seed(seed)
            np.random.seed(seed)
            random.seed(seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

        # Get the server model
        server_model = copy.deepcopy(self.get_nn_parameters())
        # Set the model to training mode
        for epoch in range(epochs):
            # train the model
            self.net.train()
            # save first training model
            # if self.args.should_save_model(round) and epoch == 0:
            self.save_model(round, self.args.get_cr_save_start_suffix())

            running_loss = 0.0
            for i, (inputs, labels) in enumerate(self.train_data_loader, 0):
                inputs, labels = inputs.to(self.device), labels.to(self.device)

                # zero the parameter gradients
                self.optimizer.zero_grad()

                # forward + backward + optimize
                outputs = self.net(inputs)
                loss = self.loss_function(outputs, labels)
                if (type in ["fed_prox", "fed_greedy"]):
                    mu = self.get_mu()
                    loss += (mu / 2) * self.diff_squared_sum(server_model)
                loss.backward()
                self.optimizer.step()

                # print statistics
                running_loss += loss.item()
                if i % self.args.get_log_interval() == 0 and epoch == epochs - 1:
                    self.args.get_logger().info(
                        '[%d, %5d] loss: %.3f' % (round, i, running_loss / self.args.get_log_interval()))

                    running_loss = 0.0

            self.scheduler.step()

        # save model
        # if self.args.should_save_model(round):
        self.save_model(round, self.args.get_cr_save_end_suffix())

        return running_loss
    def save_model(self, epoch, suffix):
        """
        Saves the model if necessary.
        """
        self.args.get_logger().debug("Saving model to flat file storage. Save #{}", epoch)

        if not os.path.exists(self.args.get_save_model_folder_path()):
            os.mkdir(self.args.get_save_model_folder_path())

        full_save_path = os.path.join(self.args.get_save_model_folder_path(), "model_" + str(self.client_idx) + "_" + str(epoch) + "_" + suffix + ".model")
        self.args.get_logger().debug("Saving model to: " + full_save_path)
        torch.save(self.get_nn_parameters(), full_save_path)



    #_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_ EVALUATION -_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_

    def calculate_class_precision(self, confusion_mat):
        """
        Calculates the precision for each class from a confusion matrix.
        """
        return numpy.diagonal(confusion_mat) / numpy.sum(confusion_mat, axis=0)

    def calculate_class_recall(self, confusion_mat):
        """
        Calculates the recall for each class from a confusion matrix.
        """
        return numpy.diagonal(confusion_mat) / numpy.sum(confusion_mat, axis=1)

    def test(self, log=False):
        self.net.eval()

        correct = 0
        total = 0
        targets_ = []
        pred_ = []
        loss = 0.0
        with torch.no_grad():
            for (images, labels) in self.test_data_loader:
                images, labels = images.to(self.device), labels.to(self.device)

                outputs = self.net(images)
                _, predicted = torch.max(outputs.data, 1)
                total += labels.size(0)
                correct += (predicted == labels).sum().item()

                targets_.extend(labels.cpu().view_as(predicted).numpy())
                pred_.extend(predicted.cpu().numpy())

                loss += self.loss_function(outputs, labels).item()

        accuracy = 100 * correct / total
        confusion_mat = confusion_matrix(targets_, pred_)

        class_precision = self.calculate_class_precision(confusion_mat)
        class_recall = self.calculate_class_recall(confusion_mat)

        if log:
            self.args.get_logger().debug('Test set: Accuracy: {}/{} ({:.0f}%)'.format(correct, total, accuracy))
            self.args.get_logger().debug('Test set: Loss: {}'.format(loss))
            self.args.get_logger().debug("Classification Report:\n" + classification_report(targets_, pred_))
            self.args.get_logger().debug("Confusion Matrix:\n" + str(confusion_mat))
            self.args.get_logger().debug("Class precision: {}".format(str(class_precision)))
            self.args.get_logger().debug("Class recall: {}".format(str(class_recall)))

        return accuracy, loss, class_precision, class_recall
