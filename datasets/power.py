import numpy as np


class POWER:
    class Data:

        def __init__(self, data):
            self.x = data.astype(np.float32)
            self.N = self.x.shape[0]

    def __init__(self, root_dir, train=True):
        self.train = train
        trn, val, tst = load_data_normalised(root_dir)
        self.trn = self.Data(trn)
        self.val = self.Data(val)
        self.tst = self.Data(tst)


    def __len__(self):
        if self.train:
            return self.trn.N
        else:
            return self.tst.N

    def __getitem__(self, idx):
        if self.train:
            return self.trn.x[index]
        else:
            return self.tst.x[inedx]


    # def __init__(self, root):
    #     trn, val, tst = load_data_normalised(root)
    #
    #     self.trn = self.Data(trn)
    #     self.val = self.Data(val)
    #     self.tst = self.Data(tst)
    #
    #     self.n_dims = self.trn.x.shape[1]


def load_data(root):
    return np.load(root + 'power/data.npy')


def load_data_split_with_noise(root):
    rng = np.random.RandomState(42)

    data = load_data(root)
    rng.shuffle(data)
    N = data.shape[0]

    data = np.delete(data, 3, axis=1)
    data = np.delete(data, 1, axis=1)
    ############################
    # Add noise
    ############################
    # global_intensity_noise = 0.1*rng.rand(N, 1)
    voltage_noise = 0.01 * rng.rand(N, 1)
    # grp_noise = 0.001*rng.rand(N, 1)
    gap_noise = 0.001 * rng.rand(N, 1)
    sm_noise = rng.rand(N, 3)
    time_noise = np.zeros((N, 1))
    # noise = np.hstack((gap_noise, grp_noise, voltage_noise, global_intensity_noise, sm_noise, time_noise))
    # noise = np.hstack((gap_noise, grp_noise, voltage_noise, sm_noise, time_noise))
    noise = np.hstack((gap_noise, voltage_noise, sm_noise, time_noise))
    data = data + noise

    N_test = int(0.1 * data.shape[0])
    data_test = data[-N_test:]
    data = data[0:-N_test]
    N_validate = int(0.1 * data.shape[0])
    data_validate = data[-N_validate:]
    data_train = data[0:-N_validate]

    return data_train, data_validate, data_test


def load_data_normalised(root):
    data_train, data_validate, data_test = load_data_split_with_noise(root)
    data = np.vstack((data_train, data_validate))
    mu = data.mean(axis=0)
    s = data.std(axis=0)
    data_train = (data_train - mu) / s
    data_validate = (data_validate - mu) / s
    data_test = (data_test - mu) / s

    return data_train, data_validate, data_test
