from colorama import init, Fore
import torch
import random
import numpy as np
from torch import nn
import matplotlib.pyplot as plt

from data.covariate import FactorModel
from models.far_nn import FactorAugmentedNN, RegressionNN
from data.fast_data import AdditiveModel, RegressionDataset
from torch.utils.data import DataLoader
from scipy.sparse.linalg.eigen.arpack import eigsh as largest_eigsh

import argparse
import time

init(autoreset=True)
parser = argparse.ArgumentParser()
parser.add_argument("--n", help="number of samples", type=int, default=500)
parser.add_argument("--m", help="number of samples to calculate the diversified "
                                "projection matrix", type=int, default=50)
parser.add_argument("--p", help="data dimension", type=int, default=1000)
parser.add_argument("--r", help="factor dimension", type=int, default=5)
parser.add_argument("--r_bar", help="diversified weight dimension", type=int, default=10)
parser.add_argument("--width", help="width of NN", type=int, default=300)
parser.add_argument("--depth", help="depth of NN", type=int, default=4)
parser.add_argument("--seed", help="random seed of numpy", type=int, default=1)
parser.add_argument("--batch_size", help="batch size", type=int, default=64)
parser.add_argument("--dropout_rate", help="dropout rate", type=float, default=0.6)
parser.add_argument("--exp_id", help="exp id", type=int, default=1)
parser.add_argument("--record_dir", help="directory to save record", type=str, default="")

args = parser.parse_args()

start_time = time.time()

# set random seed
np.random.seed(args.seed)
torch.manual_seed(args.seed)
random.seed(args.seed)

# hyper-parameters
n_train = args.n
p = args.p
batch_size = args.batch_size
width = args.width
depth = args.depth
n_test = 10000
n_valid = args.n * 3 // 10

# data generating process
regression_model = AdditiveModel(num_funcs=args.r, normalize=False)
lfm = FactorModel(p=p, r=args.r, b_f=1, b_u=1)
print(regression_model)


def far_data(n, noise_level=0.0):
    observation, factor, _ = lfm.sample(n=n, latent=True)
    x, y = observation, regression_model.sample(factor)
    noise = np.random.normal(0, noise_level, (n, 1))
    return x, factor, y + noise


# prepare dataset
x_train_obs, x_train_latent, y_train = far_data(n_train, 0.3)
x_valid_obs, x_valid_latent, y_valid = far_data(n_valid, 0.3)
x_test_obs, x_test_latent, y_test = far_data(n_test, 0)

train_obs_data = RegressionDataset(x_train_obs, y_train)
train_latent_data = RegressionDataset(x_train_latent, y_train)
train_obs_dataloader = DataLoader(train_obs_data, batch_size=batch_size, shuffle=True)
train_latent_dataloader = DataLoader(train_latent_data, batch_size=batch_size, shuffle=True)

test_obs_data = RegressionDataset(x_test_obs, y_test)
test_latent_data = RegressionDataset(x_test_latent, y_test)
test_obs_dataloader = DataLoader(test_obs_data, batch_size=batch_size, shuffle=True)
test_latent_dataloader = DataLoader(test_latent_data, batch_size=batch_size, shuffle=True)

valid_obs_data = RegressionDataset(x_valid_obs, y_valid)
valid_latent_data = RegressionDataset(x_valid_latent, y_valid)
valid_obs_dataloader = DataLoader(valid_obs_data, batch_size=batch_size, shuffle=True)
valid_latent_dataloader = DataLoader(valid_latent_data, batch_size=batch_size, shuffle=True)

# model

unlabelled_x, _, _ = far_data(args.m)
cov_mat = np.matmul(np.transpose(unlabelled_x), unlabelled_x)
eigen_values, eigen_vectors = largest_eigsh(cov_mat, args.r_bar, which='LM')
dp_matrix = eigen_vectors / np.sqrt(p)
print(f"Diversified projection matrix size {np.shape(dp_matrix)}")

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Using {device} device")
far_nn_model = FactorAugmentedNN(p=args.p, r_bar=args.r_bar, depth=depth, width=width,
                                 dp_mat=dp_matrix, fix_dp_mat=True).to(device)
oracle_nn_model = RegressionNN(d=args.r, depth=depth, width=width).to(device)
vanilla_nn_model = RegressionNN(d=args.p, depth=depth, width=width).to(device)
far_joint_nn_model = FactorAugmentedNN(p=args.p, r_bar=args.r_bar, depth=depth, width=width,
                                       dp_mat=dp_matrix, fix_dp_mat=False).to(device)
far_joint_dropout_nn_model = FactorAugmentedNN(p=args.p, r_bar=args.r_bar, depth=depth, width=width,
                                               dp_mat=dp_matrix, fix_dp_mat=False, input_dropout=True,
                                               dropout_rate=args.dropout_rate).to(device)
dropout_nn_model = RegressionNN(d=args.p, depth=depth, width=width, input_dropout=True,
                                dropout_rate=args.dropout_rate).to(device)

print(f"FAR-NN Model:\n {far_nn_model}")
print(f"Oracle-NN Model:\n {oracle_nn_model}")
print(f"Vanilla-NN Model:\n {vanilla_nn_model}")
print(f"FAR-NN Joint Training Model:\n {far_joint_nn_model}")

# training configurations
learning_rate = 1e-4
num_epoch = 250


def train_loop(data_loader, model, loss_fn, optimizer):
    loss_sum = 0
    for batch, (x, y) in enumerate(data_loader):
        pred = model(x, is_training=True)
        loss = loss_fn(pred, y)
        loss_sum += loss.item()

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    return loss_sum / len(data_loader)


def test_loop(data_loader, model, loss_fn):
    loss_sum = 0
    with torch.no_grad():
        for x, y in data_loader:
            pred = model(x, is_training=False)
            loss_sum += loss_fn(pred, y).item()
    return loss_sum / len(data_loader)


mse_loss = nn.MSELoss()
models = {
    'far-nn': far_nn_model,
    'oracle-nn': oracle_nn_model,
    'vanilla-nn': vanilla_nn_model,
    'joint-nn': far_joint_nn_model,
    'joint-dropout-nn': far_joint_dropout_nn_model,
    'dropout-nn': dropout_nn_model
}

optimizers = {}
for method_name, model in models.items():
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    optimizers[method_name] = optimizer


# setting of plot
plt.rcParams["font.family"] = "Times New Roman"
plt.rc('font', size=24)
plt.style.use('seaborn-darkgrid')
palette = plt.get_cmap('Set1')


def train_one_dim_nn():
    for epoch in range(num_epoch):
        print(f"Epoch {epoch+1}\n-------------------")
        train_data_loader = train_latent_dataloader
        train_loss = train_loop(train_data_loader, models["oracle-nn"], mse_loss, optimizers["oracle-nn"])
        test_data_loader = test_latent_dataloader
        test_loss = test_loop(test_data_loader, models["oracle-nn"], mse_loss)
        print(f" Model [oracle-nn]: train L2 error = {train_loss}, test L2 error = {test_loss}")
        # visualize output
        with torch.no_grad():
            x, y = next(iter(test_data_loader))
            pred = models['oracle-nn'](x)
            if epoch % 5 == 0:
                plt.figure(figsize=(16, 8))
                plt.scatter(x.numpy(), y.numpy(), color='red')
                plt.scatter(x.numpy(), pred.numpy(), color=palette(2))
                plt.savefig(f"figures/epoch{epoch}.pdf")
                plt.close()


def joint_train(model_names):
    colors = [Fore.RED, Fore.YELLOW, Fore.BLUE, Fore.GREEN, Fore.CYAN, Fore.LIGHTRED_EX, Fore.LIGHTYELLOW_EX,
              Fore.LIGHTBLUE_EX, Fore.LIGHTGREEN_EX, Fore.LIGHTCYAN_EX]
    best_valid, model_color = {}, {}
    for i, name in enumerate(model_names):
        best_valid[name] = 1e9
        model_color[name] = colors[i]
    test_perf = {}
    for epoch in range(num_epoch):
        if epoch % 10 == 0:
            print(f"Epoch {epoch}\n--------------------")
        for model_name in model_names:
            train_data_loader = train_latent_dataloader if (model_name == 'oracle-nn') else train_obs_dataloader
            train_loss = train_loop(train_data_loader, models[model_name], mse_loss, optimizers[model_name])
            valid_data_loader = valid_latent_dataloader if (model_name == 'oracle-nn') else valid_obs_dataloader
            valid_loss = test_loop(valid_data_loader, models[model_name], mse_loss)
            if valid_loss < best_valid[model_name]:
                best_valid[model_name] = valid_loss
                test_data_loader = test_latent_dataloader if (model_name == 'oracle-nn') else test_obs_dataloader
                test_loss = test_loop(test_data_loader, models[model_name], mse_loss)
                test_perf[model_name] = test_loss
                print(model_color[model_name] + f"Model [{model_name}]: update test loss, best valid loss = {valid_loss}, "
                      f"current test loss = {test_loss}")
            if epoch % 10 == 0:
                print(f"Model [{model_name}]: train L2 error = {train_loss}, valid L2 error = {valid_loss}")
    result = np.zeros((1, len(model_names)))
    for i, name in enumerate(model_names):
        result[0, i] = test_perf[name]
    return result


# Exp 1 Execute
if args.exp_id == 1:
    test_l2_error = joint_train(["oracle-nn", "far-nn", "vanilla-nn", "joint-nn"])
    if len(args.record_dir) > 0:
        np.savetxt(args.record_dir + f"p{args.p}s{args.seed}.csv", test_l2_error, delimiter=",")
    end_time = time.time()
    print(f"Case with p = {args.p}, seed = {args.seed} done: time = {end_time - start_time} secs")


if args.exp_id == 2:
    test_l2_error = joint_train(["oracle-nn", "far-nn", "joint-dropout-nn", "dropout-nn"])
    if len(args.record_dir) > 0:
        np.savetxt(args.record_dir + f"/p{args.p}s{args.seed}.csv", test_l2_error, delimiter=",")
    end_time = time.time()
    print(f"Case with p = {args.p}, seed = {args.seed} done: time = {end_time - start_time} secs")