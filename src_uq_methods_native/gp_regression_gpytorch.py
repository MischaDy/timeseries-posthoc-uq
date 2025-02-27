import logging

from gpytorch.variational import CholeskyVariationalDistribution, VariationalStrategy
from torch.utils.data import TensorDataset, DataLoader
from tqdm import tqdm
import gpytorch
import torch
from torch.optim.lr_scheduler import ReduceLROnPlateau

import settings
from helpers import misc_helpers

import numpy as np


torch.set_default_device(misc_helpers.get_device())
torch.set_default_dtype(torch.float32)


STORE_PLOT_EVERY_N = None
N_SAMPLES_TO_PLOT = 1600
SHOW_PLOT = False
SAVE_PLOT = False
N_INDUCING_POINTS = None
PLOT_EXT = 'png'


# noinspection PyPep8Naming
class ApproximateGP(gpytorch.models.ApproximateGP):
    def __init__(self, inducing_points, mean_type='constant'):
        """

        :param inducing_points:
        :param mean_type: one of 'constant', 'linear'
        """
        assert mean_type in ['constant', 'linear']
        variational_distribution = CholeskyVariationalDistribution(inducing_points.size(0))
        variational_strategy = VariationalStrategy(self, inducing_points, variational_distribution,
                                                   learn_inducing_locations=True)
        super(ApproximateGP, self).__init__(variational_strategy)
        if mean_type == 'constant':
            self.mean_module = gpytorch.means.ConstantMean()
        else:
            input_dim = inducing_points[0].shape[1]
            self.mean_module = gpytorch.means.LinearMean(input_dim)
        self.covar_module = gpytorch.kernels.ScaleKernel(gpytorch.kernels.keops.RBFKernel())

    def forward(self, X):
        X_mean = self.mean_module(X)
        X_covar = self.covar_module(X)
        return gpytorch.distributions.MultivariateNormal(X_mean, X_covar)


def make_plot(model, likelihood, quantiles, X_pred, y_true, infix=None):
    from make_partial_uq_plots import plot_uq_single_dataset

    y_pred, y_quantiles, _ = predict_with_gpytorch(model, likelihood, X_pred, quantiles)
    y_true = misc_helpers.tensor_to_np_array(y_true)
    uq_method = f'gp_{infix}' if infix is not None else 'gp'
    plot_uq_single_dataset(y_true, y_pred, y_quantiles, uq_method=uq_method, interval=90, is_training_data=True,
                           n_samples_to_plot=N_SAMPLES_TO_PLOT, show_plot=SHOW_PLOT, save_plot=SAVE_PLOT, ext=PLOT_EXT)


@misc_helpers.measure_runtime
def train_gpytorch(
        X_train: torch.Tensor,
        y_train: torch.Tensor,
        X_val: torch.Tensor,
        y_val: torch.Tensor,
        n_iter: int,
        use_scheduler=True,
        lr=1e-2,
        lr_patience=30,
        lr_reduction_factor=0.5,
        show_progress_bar=True,
        show_plots=True,
        show_losses_plot=True,
        save_losses_plot=True,
        n_samples_train_loss_plot=10000,
        io_helper=None,
        n_inducing_points=500,
        batch_size=1024,
        mean_type='constant',
        random_seed=42,
):
    n_devices = torch.cuda.device_count()
    logging.info('Planning to run on {} GPUs.'.format(n_devices))

    logging.info('setup dataset')
    train_dataset = TensorDataset(X_train, y_train)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=False)

    logging.info('setup models')
    if N_INDUCING_POINTS is not None:
        n_inducing_points = N_INDUCING_POINTS
    inducing_points = misc_helpers.get_random_arr_sample(X_train, n_inducing_points, safe=True)
    model = ApproximateGP(inducing_points=inducing_points, mean_type=mean_type)
    likelihood = gpytorch.likelihoods.GaussianLikelihood()
    model, likelihood = misc_helpers.objects_to_cuda(model, likelihood)
    model.train()
    likelihood.train()

    logging.info('setup meta-models')
    mll = gpytorch.mlls.PredictiveLogLikelihood(likelihood, model, num_data=y_train.size(0))
    optimizer = torch.optim.Adam([
        {'params': model.parameters()},
        {'params': likelihood.parameters()},
    ], lr=lr)
    scheduler = ReduceLROnPlateau(optimizer, patience=lr_patience, factor=lr_reduction_factor)

    logging.info('start proper training...')
    X_train_sample, y_train_sample = misc_helpers.get_random_arrs_samples(
        [X_train, y_train],
        n_samples=n_samples_train_loss_plot,
        random_seed=random_seed,
        safe=True,
    )
    train_losses, val_losses = [], []
    epochs = range(1, n_iter+1)
    if show_progress_bar:
        epochs = tqdm(epochs)
    for epoch in epochs:
        if not show_progress_bar:
            logging.info(f'epoch {epoch}/{n_iter}')
        for X_train_batch, y_train_batch in train_loader:
            model.train()
            likelihood.train()
            optimizer.zero_grad()
            y_pred_batch = model(X_train_batch)
            loss = -mll(y_pred_batch, y_train_batch).sum()
            loss.backward()
            optimizer.step()
    
        if not any([use_scheduler, show_losses_plot, save_losses_plot]):
            continue

        model.eval()
        likelihood.eval()
        with torch.no_grad(), gpytorch.settings.memory_efficient(True):
            # compute val loss
            y_pred_val = model(X_val)
            val_loss = -mll(y_pred_val, y_val).sum()
            val_losses.append(val_loss.item())
            if show_losses_plot or save_losses_plot:
                # compute train loss
                y_pred_train = model(X_train_sample)
                train_loss = -mll(y_pred_train, y_train_sample).sum()
                train_losses.append(train_loss.item())
        if use_scheduler:
            scheduler.step(val_loss)

        # noinspection PyTypeChecker
        if STORE_PLOT_EVERY_N is not None and epoch % STORE_PLOT_EVERY_N == 0:
            make_plot(model, likelihood, settings.QUANTILES, X_train[:N_SAMPLES_TO_PLOT], y_train[:N_SAMPLES_TO_PLOT],
                      infix=epoch)

    misc_helpers.plot_nn_losses(
        train_losses,
        val_losses,
        loss_skip=0,
        show_plot=show_plots and show_losses_plot,
        save_plot=save_losses_plot,
        io_helper=io_helper,
        filename='train_gpytorch',
    )

    logging.info(f"Finished training on {X_train.size(0)} data points using {n_devices} GPUs.")
    return model, likelihood


@misc_helpers.measure_runtime
def evaluate(model, likelihood, X_test, y_test):
    model.eval()
    likelihood.eval()

    with torch.no_grad(), gpytorch.settings.memory_efficient(True):
        y_pred = model.likelihood(model(X_test))

    logging.info('computing loss...')
    rmse = (y_pred.mean - y_test).square().mean().sqrt().item()
    logging.info(f"RMSE: {rmse:.3f}")
    return y_pred


def prepare_data(
        X_train: 'np.ndarray',
        y_train: 'np.ndarray',
        X_val: 'np.ndarray',
        y_val: 'np.ndarray',
        X_pred: 'np.ndarray',
):
    logging.info('preparing data..')
    X_train, y_train, X_val, y_val, X_pred = misc_helpers.np_arrays_to_tensors(
        X_train, y_train, X_val, y_val, X_pred
    )
    y_train, y_val = misc_helpers.make_arrs_1d(y_train, y_val)

    logging.info('mapping data to device and making it contiguous...')
    X_train, y_train, X_val, y_val, X_pred = misc_helpers.objects_to_cuda(X_train, y_train, X_val, y_val, X_pred)
    X_train, y_train, X_val, y_val, X_pred = misc_helpers.make_tensors_contiguous(
        X_train, y_train, X_val, y_val, X_pred
    )
    return X_train, y_train, X_val, y_val, X_pred


def predict_with_gpytorch(model, likelihood, X_pred: torch.Tensor, quantiles):
    model.eval()
    likelihood.eval()

    test_dataset = TensorDataset(X_pred)
    test_loader = DataLoader(test_dataset, batch_size=1024, shuffle=False)
    test_loader = map(lambda list_with_tensor: list_with_tensor[0], test_loader)

    y_preds_all, y_stds_all, y_quantiles_all = [], [], []
    # todo: via gpytorch.settings, use fast_pred_samples, fast_computations?
    with torch.no_grad(), gpytorch.settings.memory_efficient(True):
        for X_test_batch in test_loader:
            f_pred = model(X_test_batch)
            y_pred = f_pred.mean
            y_std = f_pred.stddev
            y_pred, y_std = misc_helpers.tensors_to_np_arrays(y_pred, y_std)
            y_pred, y_std = misc_helpers.make_arrs_2d(y_pred, y_std)
            y_quantiles = misc_helpers.quantiles_gaussian(quantiles, y_pred, y_std)

            y_preds_all.append(y_pred)
            y_stds_all.append(y_std)
            y_quantiles_all.append(y_quantiles)

    y_pred = np.vstack(y_preds_all).squeeze()
    y_std = np.vstack(y_stds_all).squeeze()
    y_quantiles = np.vstack(y_quantiles_all)
    return y_pred, y_quantiles, y_std
