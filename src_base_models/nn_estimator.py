import logging
from typing import TYPE_CHECKING
from more_itertools import collapse

# noinspection PyProtectedMember
from sklearn.base import RegressorMixin, BaseEstimator, _fit_context

import torch
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data import TensorDataset, DataLoader

from tqdm import tqdm

from helpers import misc_helpers
from helpers.early_stopper import EarlyStopper

if TYPE_CHECKING:
    import numpy as np


torch.set_default_dtype(torch.float32)


# noinspection PyAttributeOutsideInit,PyPep8Naming
class NN_Estimator(RegressorMixin, BaseEstimator):
    """
    Parameters
    ----------

    Attributes
    ----------
    is_fitted_ : bool
        A boolean indicating whether the estimator has been fitted.

    n_features_in_ : int
        Number of features seen during :term:`fit`.

    feature_names_in_ : ndarray of shape (`n_features_in_`,)
        Names of features seen during :term:`fit`. Defined only when `X`
        has feature names that are all strings.
    """

    _parameter_constraints = {
        'dim_in': [int],
        "train_size_orig": [int],
        "n_iter": [int],
        "batch_size": [int],
        "random_seed": [int],
        "lr": [float, None],
        "lr_patience": [int],
        "lr_reduction_factor": [float],
        "to_standardize": [str],
        "use_scheduler": [bool],
        "skip_training": [bool],
        "save_model": [bool],
        "verbose": [int],
        'show_progress_bar': [bool],
        'show_losses_plot': [bool],
        'save_losses_plot': [bool],
        'output_dim': [int],
        'weight_decay': [float],
        'n_samples_train_loss_plot': [int],
        'early_stop_patience': [int],
    }

    def __init__(
            self,
            dim_in,
            train_size_orig,
            n_iter=100,
            batch_size=20,
            random_seed=42,
            num_hidden_layers=2,
            hidden_layer_size=50,
            activation=torch.nn.LeakyReLU,
            weight_decay=0,
            use_scheduler=True,
            lr=None,
            lr_patience=5,
            lr_reduction_factor=0.5,
            verbose: int = 1,
            show_progress_bar=True,
            show_losses_plot=True,
            save_losses_plot=True,
            io_helper=None,
            output_dim=2,
            early_stop_patience=None,
            n_samples_train_loss_plot=10000,
    ):
        if use_scheduler and early_stop_patience is not None and early_stop_patience <= lr_patience:
            logging.warning('early stop patience < LR patience!')
        self.dim_in = dim_in
        self.train_size_orig = train_size_orig  # todo: temp solution
        self.use_scheduler = use_scheduler
        self.n_iter = n_iter
        self.batch_size = batch_size
        self.random_seed = random_seed
        self.num_hidden_layers = num_hidden_layers
        self.hidden_layer_size = hidden_layer_size
        self.activation = activation
        self.weight_decay = weight_decay
        self.lr = lr
        self.lr_patience = lr_patience
        self.lr_reduction_factor = lr_reduction_factor
        self.verbose = verbose
        self.show_progress_bar = show_progress_bar
        self.show_losses_plot = show_losses_plot
        self.save_losses_plot = save_losses_plot
        self.n_samples_train_loss_plot = n_samples_train_loss_plot
        self.io_helper = io_helper
        self.is_fitted_ = False
        self.output_dim = output_dim
        self.output_dim_orig = output_dim
        self.early_stop_patience = early_stop_patience

    @_fit_context(prefer_skip_nested_validation=True)
    def fit(self, X: 'np.ndarray', y: 'np.ndarray'):
        """
        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The training input samples.

        y : array-like, shape (n_samples,) or (n_samples, n_outputs)
            The target values (real numbers).


        Returns
        -------
        self : object
            Returns self.
        """
        """`_validate_data` is defined in the `BaseEstimator` class.
        It allows to:
        - run different checks on the input data;
        - define some attributes associated to the input data: `n_features_in_` and
          `feature_names_in_`."""
        torch.set_default_device(misc_helpers.get_device())
        torch.manual_seed(self.random_seed)

        X, y = self._validate_data(X, y, accept_sparse=False)  # todo: remove "y is 2d" warning
        assert self.dim_in == X.shape[-1]

        if self.activation is None:
            self.activation = torch.nn.LeakyReLU

        y = misc_helpers.make_arr_2d(y)
        try:
            X_train, y_train = misc_helpers.np_arrays_to_tensors(X, y)
        except TypeError:
            raise TypeError(f'Unknown label type: {X.dtype} (X) or {y.dtype} (y)')

        X_train, y_train, X_val, y_val = self._temp_train_val_split(X_train, y_train)
        X_train, y_train, X_val, y_val = misc_helpers.objects_to_cuda(X_train, y_train, X_val, y_val)
        X_train, y_train, X_val, y_val = misc_helpers.make_tensors_contiguous(X_train, y_train, X_val, y_val)

        model = self._nn_builder(
            self.dim_in,
            num_hidden_layers=self.num_hidden_layers,
            hidden_layer_size=self.hidden_layer_size,
            activation=self.activation,
        )
        model = misc_helpers.object_to_cuda(model)

        # noinspection PyTypeChecker
        train_loader = self._get_train_loader(X_train, y_train, self.batch_size)

        if self.lr is None:
            self.lr = 1e-2 if self.use_scheduler else 1e-4

        optimizer = torch.optim.Adam(model.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        scheduler = ReduceLROnPlateau(optimizer, patience=self.lr_patience, factor=self.lr_reduction_factor)
        criterion = torch.nn.MSELoss()
        criterion = misc_helpers.object_to_cuda(criterion)
        if self.early_stop_patience is not None:
            early_stopper = EarlyStopper(self.early_stop_patience)

        prev_lr = self.lr
        X_train_sample, y_train_sample = misc_helpers.get_random_arrs_samples(
            [X_train, y_train],
            n_samples=self.n_samples_train_loss_plot,
            random_seed=self.random_seed,
            safe=True,
        )
        train_losses, val_losses = [], []
        epochs = tqdm(range(self.n_iter)) if self.show_progress_bar else range(self.n_iter)
        for epoch in epochs:
            if not self.show_progress_bar:
                logging.info(f'epoch {epoch}/{self.n_iter}')
            model.train()
            for X, y in train_loader:
                optimizer.zero_grad()
                y_pred = model(X)
                loss = criterion(y_pred, y)
                loss.backward()
                optimizer.step()
            if not any([self.use_scheduler, self.show_losses_plot, self.save_losses_plot]):
                continue

            model.eval()
            with torch.no_grad():
                val_loss = self._mse_torch(model(X_val), y_val)
                val_loss_np = misc_helpers.tensor_to_np_array(val_loss)
                if self.show_losses_plot or self.save_losses_plot:
                    val_losses.append(val_loss_np)
                    train_loss_np = self._mse_torch(model(X_train_sample), y_train_sample)
                    train_loss_np = misc_helpers.tensor_to_np_array(train_loss_np)
                    train_losses.append(train_loss_np)

            logging.info(f'epoch {epoch} -- last val loss: {val_loss_np}')

            # noinspection PyUnboundLocalVariable
            if self.early_stop_patience is not None and early_stopper.should_stop(val_loss):
                logging.info(f'stopping early since no improvement in past {self.early_stop_patience} epochs')
                break

            if self.use_scheduler:
                scheduler.step(val_loss)
                new_lr = scheduler.get_last_lr()[0]
                if new_lr < prev_lr:
                    logging.info(f'reduced LR from {prev_lr} to {new_lr}')
                    prev_lr = new_lr

        model.eval()
        self.model_ = model

        loss_skip = min(100, self.n_iter // 10)
        misc_helpers.plot_nn_losses(
            train_losses,
            val_losses,
            loss_skip=loss_skip,
            show_plot=self.show_losses_plot,
            save_plot=self.save_losses_plot,
            io_helper=self.io_helper,
            filename='nn_estimator_fit',
        )

        self.is_fitted_ = True
        return self

    def _temp_train_val_split(self, X_train: 'np.ndarray', y_train: 'np.ndarray'):
        X_train, X_val = X_train[:self.train_size_orig], X_train[self.train_size_orig:]
        y_train, y_val = y_train[:self.train_size_orig], y_train[self.train_size_orig:]
        return X_train, y_train, X_val, y_val

    @staticmethod
    def _nn_builder(
            dim_in,
            num_hidden_layers=2,
            hidden_layer_size=50,
            activation=torch.nn.LeakyReLU,
    ):
        layers = collapse([
            torch.nn.Linear(dim_in, hidden_layer_size),
            activation(),
            [[torch.nn.Linear(hidden_layer_size, hidden_layer_size),
              activation()]
             for _ in range(num_hidden_layers)],
            torch.nn.Linear(hidden_layer_size, 1),
        ])
        model = torch.nn.Sequential(*layers)
        return model.float()

    @classmethod
    def _get_train_loader(cls, X_train: torch.Tensor, y_train: torch.Tensor, batch_size):
        train_dataset = TensorDataset(X_train, y_train)
        train_loader = DataLoader(train_dataset, batch_size=batch_size)
        return train_loader

    @staticmethod
    def _mse_torch(y_pred, y_test):
        return torch.mean((y_pred - y_test) ** 2)

    def predict(self, X: 'np.ndarray', as_np=True):
        """A reference implementation of a predicting function.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The training input samples.

        as_np : ...

        Returns
        -------
        y : ndarray, shape (n_samples,)
            Returns an array of ones.

        """
        from sklearn.utils.validation import check_is_fitted

        # Check if fit had been called
        check_is_fitted(self)
        # We need to set reset=False because we don't want to overwrite
        # `feature_names_in_` but only check that the shape is consistent.
        X = self._validate_data(X, accept_sparse=False, reset=False)
        X = misc_helpers.np_array_to_tensor(X)
        result = self.predict_on_tensor(X)
        result = misc_helpers.make_arr_1d(result) if self.output_dim == 1 else misc_helpers.make_arr_2d(result)
        if as_np:
            result = misc_helpers.tensor_to_np_array(result)
        return result

    def predict_on_tensor(self, X: torch.Tensor):
        X = misc_helpers.object_to_cuda(X)
        X = misc_helpers.make_tensor_contiguous(X)
        with torch.no_grad():
            result = self.model_(X)
        return result

    def get_nn(self, to_device=True) -> torch.nn.Module:
        if to_device:
            return misc_helpers.object_to_cuda(self.model_)
        return self.model_

    def _more_tags(self):
        return {'poor_score': True,
                '_xfail_checks': {'check_methods_sample_order_invariance': '(barely) failing for unknown reason'}}

    # noinspection PyUnusedLocal
    def to(self, device):
        # self.model_ = misc_helpers.object_to_cuda(self.model_)
        self.model_ = self.model_.to(device)
        return self

    def __getattr__(self, item):
        try:
            model = self.__getattribute__('model_')  # workaround bc direct attr access doesn't work
            return getattr(model, item)
        except AttributeError:
            msg = f'NN_Estimator has no attribute "{item}"'
            if not self.__getattribute__('is_fitted_'):
                msg += ', or only has it is after fitting'
            raise AttributeError(msg)

    def __call__(self, X: torch.Tensor):
        if not self.__getattribute__('is_fitted_'):
            raise TypeError('NN_Estimator is only callable after fitting')
        return self.predict_on_tensor(X)

    def __setattr__(self, key, value):
        self.__dict__[key] = value

    def set_output_dim(self, output_dim, orig=False):
        self.output_dim = output_dim
        if orig:
            self.output_dim_orig = output_dim

    def reset_output_dim(self):
        self.output_dim = self.output_dim_orig

    def load_state_dict(self, state_dict):
        model = self._nn_builder(
            self.dim_in,
            num_hidden_layers=self.num_hidden_layers,
            hidden_layer_size=self.hidden_layer_size,
            activation=self.activation,
        )
        model.load_state_dict(state_dict)
        self.model_ = model


def train_nn(
        X_train: 'np.ndarray',
        y_train: 'np.ndarray',
        X_val: 'np.ndarray',
        y_val: 'np.ndarray',
        n_iter=500,
        batch_size=20,
        random_seed=42,
        num_hidden_layers=2,
        hidden_layer_size=50,
        activation=torch.nn.LeakyReLU,
        weight_decay=0,
        lr=None,
        use_scheduler=True,
        lr_patience=30,
        lr_reduction_factor=0.5,
        show_progress_bar=True,
        show_losses_plot=True,
        save_losses_plot=True,
        io_helper=None,
        n_samples_train_loss_plot=10000,
        verbose: int = 1,
        warm_start_model=None,
        early_stop_patience=None,
) -> NN_Estimator:
    train_size_orig = X_train.shape[0]
    X_train, y_train = misc_helpers.add_val_to_train(X_train, X_val, y_train, y_val)  # todo: temp solution
    dim_in = X_train.shape[1]
    if warm_start_model is None:
        model = NN_Estimator(
            dim_in=dim_in,
            train_size_orig=train_size_orig,
            n_iter=n_iter,
            batch_size=batch_size,
            random_seed=random_seed,
            num_hidden_layers=num_hidden_layers,
            hidden_layer_size=hidden_layer_size,
            activation=activation,
            weight_decay=weight_decay,
            lr=lr,
            use_scheduler=use_scheduler,
            lr_patience=lr_patience,
            lr_reduction_factor=lr_reduction_factor,
            verbose=verbose,
            show_progress_bar=show_progress_bar,
            show_losses_plot=show_losses_plot,
            save_losses_plot=save_losses_plot,
            io_helper=io_helper,
            early_stop_patience=early_stop_patience,
            n_samples_train_loss_plot=n_samples_train_loss_plot,
        )
    else:
        model = warm_start_model
    # noinspection PyTypeChecker
    model.fit(X_train, y_train)
    return model


if __name__ == '__main__':
    from sklearn.utils.estimator_checks import check_estimator
    estimator = NN_Estimator(dim_in=2, train_size_orig=10, verbose=0)  # todo: does temp solution work?
    check_estimator(estimator)
