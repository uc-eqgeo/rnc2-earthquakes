import multiprocessing as mp
import tables as pt


def handle_output(output: mp.Queue, output_file: str):
    hdf = pt.openFile(output_file, mode='w')
    while True:
        args = output.get()
        if args:
            method, args = args
            getattr(hdf, method)(*args)
        else:
            break
    hdf.close()


