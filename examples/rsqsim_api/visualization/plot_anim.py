from rsqsim_api.containers.catalogue import RsqSimCatalogue
from rsqsim_api.containers.fault import RsqSimMultiFault
from rsqsim_api.visualisation.animation import AnimateSequence
import os
import numpy as np

run_dir = os.path.dirname(__file__)

if __name__ == "__main__":
    catalogue = RsqSimCatalogue.from_csv_and_arrays(
    os.path.join(run_dir, "../../../data/bruce_m7/bruce_m7_10kyr"))
    bruce_faults = RsqSimMultiFault.read_fault_file_bruce(os.path.join(run_dir, "../../../data/bruce_m7/zfault_Deepen.in"),
                                                        os.path.join(run_dir, "../../../data/bruce_m7/znames_Deepen.in"),
                                                        transform_from_utm=True)

    filtered_cat = catalogue.filter_whole_catalogue(
        min_t0=1000*3.154e7, max_t0=2000*3.154e7)  # 1000 years
    event_list = np.unique(filtered_cat.event_list)
    events = filtered_cat.events_by_number(event_list.tolist(), bruce_faults)
    AnimateSequence(events, write="demo")
