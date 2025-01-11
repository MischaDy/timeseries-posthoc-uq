from helpers.io_helper import IO_Helper

UQ_METHODS_WHITELIST = {
    'qhgbr',
    # 'qr',
    # 'gp',
    # 'mvnn',
    # 'cp_hgbr',
    # 'cp_linreg',
    # 'cp_nn',
    # 'la_nn',
}

UQ_METHOD_TO_ARR_NAMES_DICT = {
    'qhgbr': [
        'native_qhgbr_y_pred_n210432_it0.npy',
        'native_qhgbr_y_quantiles_n210432_it0.npy',
        'native_qhgbr_y_std_n210432_it0.npy',
    ],
    'qr': [
        'native_quantile_regression_nn_y_pred_n210432_it300_nh2_hs50.npy',
        'native_quantile_regression_nn_y_quantiles_n210432_it300_nh2_hs50.npy',
        'native_quantile_regression_nn_y_std_n210432_it300_nh2_hs50.npy',
    ],
    'gp': [
        'native_gpytorch_y_pred_n210432_it200.npy',
        'native_gpytorch_y_quantiles_n210432_it200.npy',
        'native_gpytorch_y_std_n210432_it200.npy',
    ],
    'mvnn': [
        'native_mvnn_y_pred_n210432_it100_nh2_hs50.npy',
        'native_mvnn_y_quantiles_n210432_it100_nh2_hs50.npy',
        'native_mvnn_y_std_n210432_it100_nh2_hs50.npy',
    ],
    'cp_hgbr': [
        'posthoc_conformal_prediction_base_model_hgbr_y_pred_n210432_it5.npy',
        'posthoc_conformal_prediction_base_model_hgbr_y_quantiles_n210432_it5.npy',
        'posthoc_conformal_prediction_base_model_hgbr_y_std_n210432_it5.npy'
    ],
    'cp_linreg': [
        'posthoc_conformal_prediction_base_model_linreg_y_pred_n210432_it5.npy',
        'posthoc_conformal_prediction_base_model_linreg_y_quantiles_n210432_it5.npy',
        'posthoc_conformal_prediction_base_model_linreg_y_std_n210432_it5.npy'
    ],
    'cp_nn': [
        'posthoc_conformal_prediction_base_model_nn_y_pred_n210432_it5.npy',
        'posthoc_conformal_prediction_base_model_nn_y_quantiles_n210432_it5.npy',
        'posthoc_conformal_prediction_base_model_nn_y_std_n210432_it5.npy'
    ],
    'la_nn': [
        'posthoc_laplace_approximation_base_model_nn_y_pred_n210432_it100.npy',
        'posthoc_laplace_approximation_base_model_nn_y_quantiles_n210432_it100.npy',
        'posthoc_laplace_approximation_base_model_nn_y_std_n210432_it100.npy',
    ],
}


def get_uq_method_to_arrs_dict():
    uq_method_to_arrs_dict = {uq_method: to_arrs(arr_names)
                              for uq_method, arr_names in UQ_METHOD_TO_ARR_NAMES_DICT.items()
                              if uq_method in UQ_METHODS_WHITELIST}
    return uq_method_to_arrs_dict


def to_arrs(filenames, io_helper=None, storage_path='comparison_storage'):
    if io_helper is None:
        io_helper = IO_Helper(storage_path)
    return [io_helper.load_array(filename=filename) for filename in filenames]
