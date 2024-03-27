from rsqsim_api.catalogue.catalogue import RsqSimCatalogue
from rsqsim_api.fault.multifault import RsqSimMultiFault
from rsqsim_api.visualisation.utilities import plot_coast, plot_hillshade, plot_tide_gauge
from rsqsim_api.io.rsqsim_constants import seconds_per_year
from matplotlib import pyplot as plt
from matplotlib.widgets import Slider
from matplotlib.animation import FuncAnimation, PillowWriter, FFMpegWriter
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LogNorm
from mpl_toolkits.axes_grid1 import make_axes_locatable
from concurrent.futures import ThreadPoolExecutor, as_completed, ProcessPoolExecutor, wait, FIRST_COMPLETED
import os.path
import math
import numpy as np
import pickle
import netCDF4 as nc
from io import BytesIO

from multiprocessing import Pool
from functools import partial
from time import time
from shapely.geometry import LineString

def AnimateSequence(catalogue: RsqSimCatalogue, fault_model: RsqSimMultiFault, subduction_cmap: str = "plasma",
                    crustal_cmap: str = "viridis", global_max_slip: int = 10, global_max_sub_slip: int = 40,
                    step_size: int = 1, interval: int = 50, write: str = None, fps: int = 20, file_format: str = "gif",
                    figsize: tuple = (9.6, 7.2), hillshading_intensity: float = 0.0, bounds: tuple = None,
                    pickled_background : str = None, fading_increment: float = 2.0, plot_log: bool= False,
                    log_min: float = 1., log_max: float = 100., plot_subduction_cbar: bool = True,
                    plot_crustal_cbar: bool = True, min_slip_value: float = None, plot_zeros: bool = True,
                    extra_sub_list: list = None, plot_cbars: bool = False, write_frames: bool = False,
                    pickle_plots: str = None, load_pickle_plots: str = None, num_threads: int = 4, **kwargs):
    """Shows an animation of a sequence of earthquake events over time

    Args:
        catalogue (RsqSimCatalogue): Catalogue of events to animate
        fault_model (RsqSimMultiFault): Fault model for events
        subduction_cmap (str): Colourmap for subduction colorbar
        crustal_cmap (str): Colourmap for crustal_cmap colorbar
        global_max_slip (int): Max slip to use for the colorscale
        global_max_sub_slip (int): Max subduction slip to use for the colorscale
        step_size (int): Step size to advance every interval
        interval (int): Time (ms) between each frame
        write (str): Write animation to file with given filename.
        fps (int): Frames per second.
        file_format (str): File extension for animation. Accepted values: gif, mp4, mov, avi.
        figsize (float, float): Width, height in inches.
        hillshading_intensity (float): Intensity of hillshading, value between 0-1.
    """
    assert file_format in ("gif", "mov", "avi", "mp4")

    # get all unique values
    event_list = np.unique(catalogue.event_list)
    # get RsqSimEvent objects
    events = catalogue.events_by_number(event_list.tolist(), fault_model)

    if pickled_background is not None:
        with open(pickled_background, "rb") as pfile:
            loaded_subplots = pickle.load(pfile)
        fig, background_ax = loaded_subplots
        coast_ax = background_ax["main_figure"]
        slider_ax = background_ax["slider"]
        year_ax = background_ax["year"]
    else:
        fig = plt.figure(figsize=figsize)

        # plot map
        coast_ax = fig.add_subplot(111, label="coast")
        if hillshading_intensity > 0:
            plot_coast(coast_ax, colors="0.0")
        else:
            plot_coast(coast_ax)

    coast_ax.set_aspect("equal")
    coast_ax.patch.set_alpha(0)
    coast_ax.get_xaxis().set_visible(False)
    coast_ax.get_yaxis().set_visible(False)

    if pickled_background is None:
        if hillshading_intensity > 0:
            x_lim = coast_ax.get_xlim()
            y_lim = coast_ax.get_ylim()
            plot_hillshade(coast_ax, hillshading_intensity)
            coast_ax.set_xlim(x_lim)
            coast_ax.set_ylim(y_lim)

    num_events = len(events)

    conditions_for_load_pickle_plots = load_pickle_plots is not None and os.path.exists(load_pickle_plots)
    if conditions_for_load_pickle_plots:
        if os.path.exists(load_pickle_plots):
            with open(load_pickle_plots, "rb") as pfile:
                loaded_subplots = pickle.load(pfile)
            fig, background_ax, coast_ax, slider_ax, year_ax, all_plots, timestamps = loaded_subplots
            print("Loaded plots from pickle file")

    else:
        all_plots = []
        timestamps = []
        for i, e in enumerate(events):
            plots = e.plot_slip_2d(
                subplots=(fig, coast_ax), global_max_slip=global_max_slip, global_max_sub_slip=global_max_sub_slip,
                bounds=bounds, plot_log_scale=plot_log, log_min=log_min, log_max=log_max, min_slip_value=min_slip_value,
                plot_zeros=plot_zeros, extra_sub_list=extra_sub_list, plot_cbars=plot_cbars)
            for p in plots:
                p.set_visible(False)
            years = math.floor(e.t0 / (3.154e7))
            all_plots.append(plots)
            timestamps.append(step_size * round(years/step_size))
            print("Plotting: " + str(i + 1) + "/" + str(num_events))

        if pickle_plots is not None:
            with open(pickle_plots, "wb") as pfile:
                pickle.dump((fig, background_ax, coast_ax, slider_ax, year_ax, all_plots, timestamps), pfile)




    time_slider_all = Slider(
        slider_ax, 'Year', timestamps[0] - step_size, timestamps[-1] + step_size,
        valinit=timestamps[0] - step_size, valstep=step_size)
    frames = int((time_slider_all.valmax - time_slider_all.valmin) / step_size) + 1

    if num_threads > 1:
        split_frames = np.array_split(np.arange(frames), num_threads)
        arg_holder = []
        with ProcessPoolExecutor(max_workers=num_threads) as plot_executor:
            for i in range(num_threads):
                print(f"Starting thread {i}")
                with open(load_pickle_plots, "rb") as pfile:
                    loaded_subplots = pickle.load(pfile)
                arg_holder.append(loaded_subplots)
                fig, background_ax, coast_ax, slider_ax, year_ax, all_plots_i, timestamps_i = arg_holder[i]
                pickled_figure = fig, background_ax, coast_ax, slider_ax, year_ax
                pool_kwargs = { "step_size": step_size, "interval": interval,
                                "write": write, "write_frames": write_frames, "file_format": file_format, "fps": fps,
                                "fading_increment": fading_increment, "figsize": figsize,
                                "hillshading_intensity": hillshading_intensity}


                plot_executor.submit(plot_axis_sequence, split_frames[i], timestamps_i, all_plots_i, pickled_figure,
                                     **pool_kwargs)

    else:
        pickled_figure = fig, background_ax, coast_ax, slider_ax, year_ax
        plot_axis_sequence(frames, pickled_background=pickled_figure, timestamps=timestamps, all_plots=all_plots,
                            step_size=step_size, interval=interval, write=write, write_frames=write_frames,
                            file_format=file_format, fps=fps, fading_increment=fading_increment, figsize=figsize,
                            hillshading_intensity=hillshading_intensity)


class AxesSequence(object):
    """Controls a series of plots on the screen and when they are visible"""

    def __init__(self, fig, timestamps, plots, coast_ax, fading_increment: float = 2.0):
        self.fig = fig
        self.timestamps = timestamps
        self.plots = plots
        self.coast_ax = coast_ax
        self.on_screen = []  # earthquakes currently displayed
        self._i = -1  # Currently displayed axes index
        self.fading_increment = fading_increment

    def set_plot(self, val):
        # plot corresponding event
        while self._i < len(self.timestamps) - 1 and val == self.timestamps[self._i + 1]:
            self._i += 1
            curr_plots = self.plots[self._i]
            print(curr_plots)
            for p in curr_plots:
                p.set_visible(True)
            self.on_screen.append(curr_plots)
            print(self.on_screen)

        for i, p in enumerate(self.on_screen):
            self.fade(p, i)

    def fade(self, plot, index):
        visible = True
        for p in plot:
            opacity = p.get_alpha()
            if opacity / 2 <= 1e-2:
                p.set_alpha(1)
                visible = False
                p.set_visible(False)
            else:
                p.set_alpha(opacity / self.fading_increment)
        if not visible:
            self.on_screen.pop(index)

    def stop(self):
        for plot in self.on_screen:
            for p in plot:
                p.set_visible(False)
                p.set_alpha(1)
        self._i = -1
        self.on_screen.clear()

    def show(self):
        plt.show()


def plot_axis_sequence(frames, timestamps, all_plots, pickled_background, step_size=1,
                       interval=50, write=None, write_frames=False, file_format="gif", fps=20, fading_increment=2.0,
                       figsize: tuple = (9.6, 7.2), hillshading_intensity: float = 0.0):
    """Controls a series of plots on the screen and when they are visible"""

    if pickled_background is not None:

        fig, background_ax, coast_ax, slider_ax, year_ax = pickled_background
        coast_ax = background_ax["main_figure"]
        slider_ax = background_ax["slider"]
        year_ax = background_ax["year"]
        year_text = year_ax.text(0.5, 0.5, str(int(0)), horizontalalignment='center', verticalalignment='center',
                                 fontsize=12)
    else:
        fig = plt.figure(figsize=figsize)

        # plot map
        coast_ax = fig.add_subplot(111, label="coast")
        if hillshading_intensity > 0:
            plot_coast(coast_ax, colors="0.0")
        else:
            plot_coast(coast_ax)

    coast_ax.set_aspect("equal")
    coast_ax.patch.set_alpha(0)
    coast_ax.get_xaxis().set_visible(False)
    coast_ax.get_yaxis().set_visible(False)

    if pickled_background is None:
        if hillshading_intensity > 0:
            x_lim = coast_ax.get_xlim()
            y_lim = coast_ax.get_ylim()
            plot_hillshade(coast_ax, hillshading_intensity)
            coast_ax.set_xlim(x_lim)
            coast_ax.set_ylim(y_lim)



    time_slider = Slider(
        slider_ax, 'Year', timestamps[0] - step_size, timestamps[-1] + step_size, valinit=timestamps[0] - step_size, valstep=step_size)
    time_slider.valtext.set_visible(False)

    axes = AxesSequence(fig, timestamps, all_plots, coast_ax, fading_increment=fading_increment)
    print(all_plots)

    def update(val):
        time = time_slider.val
        axes.set_plot(time)
        if val == time_slider.valmax:
            axes.stop()
        year_text.set_text(str(int(time)))
        fig.canvas.draw_idle()

    time_slider.on_changed(update)

    def update_plot(num):
        val = time_slider.valmin + num * step_size
        time_slider.set_val(val)

    if write_frames:
        for i in range(frames):
            update_plot(i)
            fig.savefig(f"frames/frame{i:04d}.png", dpi=300)
    else:
        animation = FuncAnimation(fig, update_plot,
                                  interval=interval, frames=frames)

        if write is not None:
            writer = PillowWriter(fps=fps) if file_format == "gif" else FFMpegWriter(fps=fps)
            animation.save(f"{write}.{file_format}", writer, dpi=300)
        else:
            axes.show()


def write_animation_frame(frame_num, frame_time, start_time, end_time, step_size, catalogue: RsqSimCatalogue, fault_model: RsqSimMultiFault,
                          pickled_background: str,
                           subduction_cmap: str = "plasma", crustal_cmap: str = "viridis", global_max_slip: int = 10,
                           global_max_sub_slip: int = 40,
                           bounds: tuple = None, fading_increment: float = 2.0, time_to_threshold: float = 10.,
                           plot_log: bool = False, log_min: float = 1., log_max: float = 100.,
                           min_slip_value: float = None, plot_zeros: bool = True, extra_sub_list: list = None,
                           min_mw: float = None, decimals: int = 1, subplot_name: str = "main_figure",
                           displace: bool = False, disp_slip_max: float = 10., cum_slip_max: list = [5., 10.], disp_map_dir: str = None, tide: dict = None):
    """
    Writes a single frame of an animation to file
    """
    begin = time()
    if frame_time - time_to_threshold < 0:
        time_to_threshold = frame_time  # Bodge to ensure that not searching catalogue for events before the start

    frame_time_seconds = frame_time * seconds_per_year
    shortened_cat = catalogue.filter_df(min_t0=frame_time_seconds - time_to_threshold * seconds_per_year,
                                        max_t0=frame_time_seconds,
                                        min_mw=min_mw).copy(deep=True)

    disp_cats = [shortened_cat]
    if displace:    # Create Catalogue of events for cumulative displacements
        aniDir = os.path.dirname(pickled_background)
        slipDir = os.path.join(aniDir, 'slip')
        cum1Dir = os.path.join(aniDir, 'cum1')
        cum2Dir = os.path.join(aniDir, 'cum2')
        cum_ax = ['ud1', 'ud2', 'ud3']
        cumDirs = [slipDir, cum1Dir, cum2Dir]
    
    Lon = np.load(os.path.join(aniDir, 'Lon.npy'))
    Lat = np.load(os.path.join(aniDir, 'Lat.npy'))

    if shortened_cat.empty:  # Plot boring frames
    #    return frame_num, None
        loaded_subplots = pickle.load(open(pickled_background, "rb"))

        fig, axes = loaded_subplots
        slider_ax = axes["slider"]
        time_slider = Slider(
            slider_ax, 'Year', start_time - step_size[0], end_time + step_size[0], valinit=start_time - step_size[0],
            valstep=step_size[0])
        time_slider.valtext.set_visible(False)
        year_ax = axes["year"]
        year_text = year_ax.text(0.5, 0.5, str(int(0)), horizontalalignment='center', verticalalignment='center',
                                fontsize=12)
        if decimals == 0:
            year_text.set_text(str(int(round(frame_time, 0))))
        else:
            year_text.set_text(f"{frame_time:.{decimals}f}")
        time_slider.set_val(frame_time)

        if displace: # Plot cumulative displacements (Check needed as cumulative window can be larger than earthquake fading time)
            for ix, cum in enumerate(cum_ax[1:]):
                disp_file = os.path.join(cumDirs[ix + 1], f'disp_{frame_num}.npy')
                if os.path.exists(disp_file):
                    disp_cum = np.load(disp_file)
                    plot_uplift(subplots=(fig, axes[cum]), disp_max=cum_slip_max[ix], bounds=bounds, disp=np.flipud(disp_cum), Lon=Lon, Lat=Lat)
                
        if tide['time'] > 0:
            plot_tide_gauge((fig, axes['ud1'], axes['tg']), tide, frame_time, start_time, step_size[0])
        
        print('Frame {} Total Time: {:.5f} seconds\n\n'.format(frame_num, time() - begin))

        return frame_num, fig

    else:  # Plot event frames
        loaded_subplots = pickle.load(open(pickled_background, "rb"))

        fig, axes = loaded_subplots
        slider_ax = axes["slider"]
        time_slider = Slider(
            slider_ax, 'Year', start_time - step_size[0], end_time + step_size[0], valinit=start_time - step_size[0],
            valstep=step_size[0])
        time_slider.valtext.set_visible(False)
        year_ax = axes["year"]
        year_text = year_ax.text(0.5, 0.5, str(int(0)), horizontalalignment='center', verticalalignment='center',
                                fontsize=12)
        if decimals == 0:
            year_text.set_text(str(int(round(frame_time, 0))))
        else:
            year_text.set_text(f"{frame_time:.{decimals}f}")
        time_slider.set_val(frame_time)

        shortened_cat["diff_t0"] = np.abs(shortened_cat["t0"] - frame_time_seconds)
        sorted_indices = shortened_cat.sort_values(by="diff_t0", ascending=False).index
        events_for_plot = catalogue.events_by_number(sorted_indices.tolist(), fault_model)

        for event in events_for_plot:
            alpha = calculate_alpha((frame_time - event.t0  / seconds_per_year), fading_increment)

            event.plot_slip_2d(subplots=(fig, axes[subplot_name]), global_max_slip=global_max_slip,
                            global_max_sub_slip=global_max_sub_slip, bounds=bounds, plot_log_scale=plot_log,
                            log_min=log_min, log_max=log_max, min_slip_value=min_slip_value, plot_zeros=plot_zeros,
                            extra_sub_list=extra_sub_list, alpha=alpha)
            print('EQ Frame: {}, Event magnitude: {:.2f}, year: {:.2f}, alpha: {:.3f}'.format(frame_num, event.mw, event.t0 / seconds_per_year, alpha))
        
        if displace:  # Plot displacement map of events shown in slip rate plot
            cum_slip_max = [disp_slip_max] + cum_slip_max
            plot_faults = [True, False, False]
            for ix, cum in enumerate(cum_ax):
                disp_file = os.path.join(cumDirs[ix], f'disp_{frame_num}.npy')
                if os.path.exists(disp_file):
                    disp_cum = np.load(disp_file)
                    plot_uplift(subplots=(fig, axes[cum]), disp_max=cum_slip_max[ix], bounds=bounds, disp=np.flipud(disp_cum), Lon=Lon, Lat=Lat)

        if tide['time'] > 0:
            plot_tide_gauge((fig, axes['ud1'], axes['tg']), tide, frame_time, start_time, step_size[0])
        print('Frame {} Total Time: {:.5f} seconds\n\n'.format(frame_num, time() - begin))
        return frame_num, fig


def write_animation_frames(start_time, end_time, step_size, catalogue: RsqSimCatalogue, fault_model: RsqSimMultiFault,
                            pickled_background: str, subduction_cmap: str = "plasma", crustal_cmap: str = "viridis",
                            global_max_slip: int = 10, global_max_sub_slip: int = 40,
                            bounds: tuple = None, fading_increment: float = 2.0, time_to_threshold: float = 10.,
                            plot_log: bool = False, log_min: float = 1., log_max: float = 100.,
                            min_slip_value: float = None, plot_zeros: bool = False, extra_sub_list: list = None,
                            min_mw: float = None, decimals: int = 1, subplot_name: str = "main_figure",
                            num_threads_plot: int = 4, frame_dir: str = "frames",
                            displace: bool = False, disp_slip_max: float = 10.0, cum_slip_max: float = 5.,
                            disp_map_dir: str = None, tide: dict = None):
        """
        Writes all the frames of an animation to file
        """
        steps = np.arange(start_time, end_time + step_size[0], step_size[0])
        frames = np.arange(len(steps))
        pool_kwargs = { "catalogue": catalogue, "fault_model": fault_model,
                       "pickled_background": pickled_background, "subduction_cmap": subduction_cmap,
                       "crustal_cmap": crustal_cmap, "global_max_slip": global_max_slip,
                       "global_max_sub_slip": global_max_sub_slip, "bounds": bounds,
                       "fading_increment": fading_increment, "time_to_threshold": time_to_threshold,
                       "plot_log": plot_log, "log_min": log_min, "log_max": log_max,
                       "min_slip_value": min_slip_value, "plot_zeros": plot_zeros,
                       "extra_sub_list": extra_sub_list, "min_mw": min_mw, "decimals": decimals,
                       "subplot_name": subplot_name, "displace": displace, "disp_slip_max": disp_slip_max,
                       "cum_slip_max": cum_slip_max, "disp_map_dir": disp_map_dir, "tide": tide}
        
        no_earthquakes = []
        frame_time_dict = {frame_i: frame_time for frame_i, frame_time in enumerate(steps)}
        frame_block_size = 500
        block_starts = np.arange(0, len(steps), frame_block_size)
        
        def handle_output(future):
            frame_i, fig_i = future.result()
                    
            if fig_i is not None:
                fig_i.savefig(f"{frame_dir}/frame{frame_i:04d}.png", format="png", dpi=100)
                plt.close(fig_i)
                # print(f"Writing {frame_i}")
                
            else:
                no_earthquakes.append(frame_i)

        if displace:
            print('Writing Displacement Frame Grids')
            disp_kwargs = { "catalogue": catalogue, "fault_model": fault_model,
                       "fading_increment": fading_increment, "time_to_threshold": time_to_threshold,
                       "min_mw": min_mw, "disp_map_dir": disp_map_dir}
            
            aniDir = os.path.dirname(frame_dir)
            slipDir = os.path.join(aniDir, 'slip')
            cum1Dir = os.path.join(aniDir, 'cum1')
            cum2Dir = os.path.join(aniDir, 'cum2')
            for dir in [slipDir, cum1Dir, cum2Dir]:
                if not os.path.exists(dir):
                    os.makedirs(dir)
            
            for start, end in zip(block_starts, block_starts + frame_block_size):
                with ThreadPoolExecutor(max_workers=num_threads_plot) as plot_executor:
                    for frame_i, frame_time in zip(frames[start:end], steps[start:end]):
                        if not os.path.exists(f"{frame_dir}/frame{frame_i:04d}.png"):
                            plot_executor.submit(write_displacement_grids, frame_i, frame_time, step_size, aniDir, **disp_kwargs)


        print('\nCreating Earthquake Frames')
        for start, end in zip(block_starts, block_starts + frame_block_size):
            with ThreadPoolExecutor(max_workers=num_threads_plot) as plot_executor:
                for frame_i, frame_time in zip(frames[start:end], steps[start:end]):
                    if not os.path.exists(f"{frame_dir}/frame{frame_i:04d}.png"):
                         submitted = plot_executor.submit(write_animation_frame, frame_i, frame_time, start_time, end_time, step_size, **pool_kwargs)
                         submitted.add_done_callback(handle_output)


def write_displacement_grids(frame_num, frame_time, step_size, aniDir, catalogue: RsqSimCatalogue, fault_model: RsqSimMultiFault,
                           fading_increment: float = 2.0, time_to_threshold: float = 10.,
                           min_mw: float = None, disp_map_dir: str = None):
    """
    Writes a displacement grids for each frame
    """
    begin = time()
    # Search catalogue for each event within the frame
    if frame_time - time_to_threshold < 0:
        time_to_threshold = frame_time  # Bodge to ensure that not searching catalogue for events before the start

    frame_time_seconds = frame_time * seconds_per_year
    shortened_cat = catalogue.filter_df(min_t0=frame_time_seconds - time_to_threshold * seconds_per_year,
                                        max_t0=frame_time_seconds,
                                        min_mw=min_mw).copy(deep=True)

    disp_cats = []
    for cum_time in step_size[1:]:
        if frame_time - cum_time < 0:
            cum_time = frame_time
    
        disp_cats.append(catalogue.filter_df(min_t0=frame_time_seconds - cum_time * seconds_per_year,
                                        max_t0=frame_time_seconds,
                                        min_mw=min_mw).copy(deep=True))
    
    slipDir = os.path.join(aniDir, 'slip')
    cum1Dir = os.path.join(aniDir, 'cum1')
    cum2Dir = os.path.join(aniDir, 'cum2')

    cumDirs = [cum1Dir, cum2Dir]

    # Calculate cumulative displacements
    for ix, disp_cat in enumerate(disp_cats):
        if not disp_cat.empty:
            disp_cat["diff_t0"] = np.abs(disp_cat["t0"] - frame_time_seconds)
            sorted_indices = disp_cat.sort_values(by="diff_t0", ascending=False).index
            events_for_plot = catalogue.events_by_number(sorted_indices.tolist(), fault_model)
            temp_disp = nc.Dataset(os.path.join(disp_map_dir, "ev"+str(events_for_plot[0].event_id)+".grd"))
            dispX = temp_disp['x'][:].data
            dispY = temp_disp['y'][:].data
            disp_cum = np.zeros_like(np.array(temp_disp["z"])) * np.nan

            for event in events_for_plot:
                grdfile = os.path.join(disp_map_dir, "ev"+str(event.event_id)+".grd")
                if os.path.exists(grdfile):
                    event_disp = np.array(nc.Dataset(grdfile)["z"])
                    no_nan = np.where(~np.isnan(event_disp))
                    try:
                        disp_cum[no_nan] = np.nansum([disp_cum[no_nan], event_disp[no_nan]], axis=0)
                    except IndexError:
                        raise IndexError(f'{grdfile} likely different resolution to other displacement maps')

            np.save(os.path.join(cumDirs[ix], f'disp_{frame_num}.npy'), disp_cum)
            if not os.path.exists(os.path.join(aniDir, 'Lon.npy')):
                np.save(os.path.join(aniDir, 'Lon.npy'), dispX)
                np.save(os.path.join(aniDir, 'Lat.npy'), dispY)

    if not shortened_cat.empty:  # Plot slip displacements
        shortened_cat["diff_t0"] = np.abs(shortened_cat["t0"] - frame_time_seconds)
        sorted_indices = shortened_cat.sort_values(by="diff_t0", ascending=False).index
        events_for_plot = catalogue.events_by_number(sorted_indices.tolist(), fault_model)

        # Prepare array of cumulative displacements
        grdfile = os.path.join(disp_map_dir, "ev"+str(events_for_plot[0].event_id)+".grd")
        if os.path.exists(grdfile):
            temp_disp = nc.Dataset(grdfile)
            dispX = temp_disp['x'][:].data
            dispY = temp_disp['y'][:].data
            disp_cum = np.zeros_like(np.array(temp_disp["z"])) * np.nan

        for event in events_for_plot:
            alpha = calculate_alpha((frame_time - event.t0  / seconds_per_year), fading_increment)

            # Create frame displacement map, with fading alpha
            grdfile = os.path.join(disp_map_dir, "ev"+str(event.event_id)+".grd")
            if os.path.exists(grdfile):
                event_disp = np.array(nc.Dataset(grdfile)["z"])
                no_nan = np.where(~np.isnan(event_disp))
                disp_cum[no_nan] = np.nansum([disp_cum[no_nan], alpha * event_disp[no_nan]], axis=0)

        np.save(os.path.join(slipDir, f'disp_{frame_num}.npy'), disp_cum)
    print('Frame: {} ({:.2f} seconds)'.format(frame_num, time() - begin))


def plot_uplift(disp_cmap: str = "bwr", disp_max: float = 10., subplots=None, bounds: tuple = None, disp: list = None,
                Lon: list = None, Lat: list = None, min_trans = 0):

    # Assume matplotlib objects
    fig, ax = subplots
    plots = []
    dx = np.diff(Lon)[0] / 2
    dy = np.diff(Lat)[0] / 2

    transparencies = abs(disp) / disp_max
    transparencies[transparencies > 1] = 1
    transparencies[transparencies < min_trans] = min_trans
    transparencies[np.isnan(transparencies)] = 1

    disp_plot = ax.imshow(disp, vmin=-disp_max, vmax=disp_max, cmap=disp_cmap,
                            extent=[Lon[0] - dx, Lon[-1] + dx, Lat[0] - dy, Lat[-1] + dy],
                            zorder=1, alpha=transparencies)
    plots.append(disp_plot)

    return plots


def calculate_alpha(time_since_new, fading_increment):
    alpha = 1 / (fading_increment ** time_since_new)
    if alpha > 1:
        alpha = 1.
    return alpha


def calculate_fading_increment(time_to_threshold, threshold):
    return (1 / threshold) ** (1 / time_to_threshold)
