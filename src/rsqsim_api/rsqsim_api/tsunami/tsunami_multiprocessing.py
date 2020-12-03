from rsqsim_api.containers.fault import RsqSimMultiFault, RsqSimSegment
import multiprocessing as mp
from typing import Union
import h5py
import netCDF4 as nc
import numpy as np
from mpi4py import MPI
sentinel = None


def multiprocess_gf_to_hdf(fault: Union[RsqSimSegment, RsqSimMultiFault], x_sites: np.ndarray, y_sites: np.ndarray,
                           out_file: str, z_sites: np.ndarray = None, slip_magnitude: Union[float, int] = 1.):
    # Check sites arrays
    assert all([isinstance(a, np.ndarray) for a in [x_sites, y_sites]])
    assert x_sites.shape == y_sites.shape
    assert x_sites.ndim <= 2

    if z_sites is not None:
        assert isinstance(z_sites, np.ndarray)
        assert z_sites.shape == x_sites.shape
    else:
        z_sites = np.zeros(x_sites.shape)

    n_patches = len(fault.patch_dic)

    if x_sites.ndim == 2:
        x_array = x_sites.flatten()
        y_array = y_sites.flatten()
        z_array = z_sites.flatten()
        dset_shape = (n_patches, x_sites.shape[0], x_sites.shape[1])
    else:
        x_array = x_sites
        y_array = y_sites
        z_array = z_sites
        dset_shape = (n_patches, x_sites.size)

    num_processes = int(np.round(mp.cpu_count() / 2))
    jobs = []
    out_queue = mp.Queue()
    in_queue = mp.Queue()
    output_proc = mp.Process(target=handle_output, args=(out_queue, out_file, dset_shape))
    output_proc.start()

    for i in range(num_processes):
        p = mp.Process(target=patch_greens_functions,
                       args=(in_queue, x_array, y_array, z_array, out_queue, dset_shape, slip_magnitude))
        jobs.append(p)
        p.start()

    if isinstance(fault, RsqSimSegment):
        for patch_i, patch in enumerate(fault.patch_outlines):
            in_queue.put((patch_i, patch))
    else:
        for patch_i, patch in fault.patch_dic.items():
            in_queue.put((patch_i, patch))

    for i in range(num_processes):
        in_queue.put(sentinel)

    for p in jobs:
        p.join()

    out_queue.put(None)

    output_proc.join()


def handle_output(output_queue: mp.Queue, output_file: str, dset_shape: tuple):
    f = h5py.File(output_file, "w")
    disp_dset = f.create_dataset("ssd_1m", shape=dset_shape, dtype="f")

    while True:
        args = output_queue.get()
        if args:
            index, vert_disp = args
            disp_dset[index] = vert_disp
        else:
            break
    f.close()


def patch_greens_functions(in_queue: mp.Queue, x_sites: np.ndarray, y_sites: np.ndarray,
                           z_sites: np.ndarray,
                           out_queue: mp.Queue, grid_shape: tuple, slip_magnitude: Union[int, float] = 1):
    while True:
        queue_contents = in_queue.get()
        if queue_contents:
            index, patch = queue_contents
            print(patch.patch_number, out_queue.qsize())

            out_queue.put((index, patch.calculate_tsunami_greens_functions(x_sites, y_sites, z_sites, grid_shape,
                                                                           slip_magnitude=slip_magnitude)))
        else:
            break
