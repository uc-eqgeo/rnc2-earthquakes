import os
from collections.abc import Iterable
from typing import Union, List

import meshio
import numpy as np
import pandas as pd
from fault_mesh_tools.faultmeshops.faultmeshops import fit_plane_to_points
from matplotlib import pyplot as plt
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon
from shapely.ops import linemerge, unary_union

import rsqsim_api.io.rsqsim_constants as csts
from rsqsim_api.fault.patch import RsqSimTriangularPatch, RsqSimGenericPatch, cross_3d, norm_3d
from rsqsim_api.fault.utilities import optimize_point_spacing, calculate_dip_direction, reverse_bearing, fit_2d_line
from rsqsim_api.io.read_utils import read_dxf, read_stl
from rsqsim_api.io.tsurf import tsurf

transformer_utm2nztm = Transformer.from_crs(32759, 2193, always_xy=True)


class DisplacementArray:
    def __init__(self, x_array: np.ndarray, y_array: np.ndarray, z_array: np.ndarray = None,
                 e_array: np.ndarray = None, n_array: np.ndarray = None, v_array: np.ndarray = None):
        assert x_array.shape == y_array.shape, "X and Y arrays should be the same size"
        assert x_array.ndim == 1, "Expecting 1D arrays"
        assert not all([a is None for a in [e_array, n_array, v_array]]), "Read in at least one set of displacements"

        self.x, self.y = x_array, y_array
        if z_array is None:
            self.z = np.zeros(self.x.shape)
        else:
            assert isinstance(z_array, np.ndarray)
            assert z_array.shape == self.x.shape
            self.z = z_array

        if e_array is not None:
            assert isinstance(e_array, np.ndarray)
            assert e_array.shape == self.x.shape
        self.e = e_array

        if n_array is not None:
            assert isinstance(n_array, np.ndarray)
            assert n_array.shape == self.x.shape
        self.n = n_array

        if v_array is not None:
            assert isinstance(v_array, np.ndarray)
            assert v_array.shape == self.x.shape
        self.v = v_array


class RsqSimSegment:
    def __init__(self, segment_number: int, patch_type: str = "triangle", fault_name: str = None):
        """

        :param segment_number:
        :param patch_type:
        :param fault_name:
        """
        self._name = None
        self._patch_numbers = None
        self._patch_outlines = None
        self._patch_vertices = None
        self._vertices = None
        self._triangles = None
        self._edge_lines = None
        self._segment_number = segment_number
        self._patch_type = None
        self._adjacency_map = None
        self._laplacian = None
        self._boundary = None
        self._mean_slip_rate = None
        self._dip_dir = None
        self._trace = None
        self._mean_dip = None

        self.patch_type = patch_type
        self.name = fault_name
        self.ss_gf, self.ds_gf = (None,) * 2

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, fault_name: str):
        if fault_name is None:
            self._name = None
        else:
            assert isinstance(fault_name, str)
            assert " " not in fault_name, "No spaces in fault name, please..."
            self._name = fault_name.lower()

    @property
    def patch_numbers(self):
        return self._patch_numbers

    @patch_numbers.setter
    def patch_numbers(self, numbers: Union[list, tuple, np.ndarray]):
        number_array = np.array(numbers)
        assert number_array.dtype == "int"
        if self.patch_outlines is not None:
            assert len(number_array) == len(self.patch_outlines)
        self._patch_numbers = number_array

    @property
    def segment_number(self):
        return self._segment_number

    @property
    def patch_type(self):
        return self._patch_type

    @patch_type.setter
    def patch_type(self, patch_type: str):
        assert isinstance(patch_type, str)
        patch_lower = patch_type.lower()
        assert patch_lower in ("triangle", "rectangle", "tri", "rect"), "Expecting 'triangle' or 'rectangle'"
        if patch_lower in ("triangle", "tri"):
            self._patch_type = "triangle"
        else:
            self._patch_type = "rectangle"

    @property
    def patch_outlines(self):
        return self._patch_outlines

    @property
    def patch_vertices(self):
        return self._patch_vertices

    @property
    def patch_vertices_flat(self):
        return np.array(self.patch_vertices).reshape((len(self.patch_outlines), 1, 9)).squeeze()

    @patch_outlines.setter
    def patch_outlines(self, patches: List):
        if self.patch_type == "triangle":
            assert all([isinstance(patch, RsqSimTriangularPatch) for patch in patches])
        elif self.patch_type == "rectangle":
            assert all([isinstance(patch, RsqSimGenericPatch) for patch in patches])
        else:
            raise ValueError("Set patch type (triangle or rectangle) for fault!")

        self._patch_outlines = patches
        self._patch_vertices = [patch.vertices for patch in patches]

    @property
    def patch_triangle_rows(self):
        return np.array([triangle.flatten() for triangle in self.patch_vertices])

    @property
    def vertices(self):
        if self._vertices is None:
            self.get_unique_vertices()
        return self._vertices

    @property
    def bounds(self):
        """
        Square box in XY plane containing all vertices
        """
        x0 = min(self.vertices[:, 0])
        y0 = min(self.vertices[:, 1])
        x1 = max(self.vertices[:, 0])
        y1 = max(self.vertices[:, 1])
        bounds = np.array([x0, y0, x1, y1])
        return bounds

    @property
    def boundary(self):
        return self._boundary

    @boundary.setter
    def boundary(self, boundary_array: np.ndarray):
        if boundary_array is not None:
            assert isinstance(boundary_array, np.ndarray)
            assert boundary_array.ndim == 2  # 2D array
            assert boundary_array.shape[1] == 3  # Three columns

        self._boundary = boundary_array

    @property
    def quaternion(self):
        return None

    @property
    def mean_dip(self):
        if self._mean_dip is None:
            self.get_mean_dip()

        return self._mean_dip

    def get_mean_dip(self):
        cum_dip = []
        for patch in self.patch_outlines:
            cum_dip.append(patch.dip)
        self._mean_dip = np.mean(cum_dip)

    @property
    def mean_slip_rate(self):
        if self._mean_slip_rate is None:
            self.get_mean_slip_rate()

        return self._mean_slip_rate

    def get_mean_slip_rate(self):
        all_patches = []
        for patch in self.patch_outlines:
            slip_rate = patch.total_slip
            all_patches.append(slip_rate)

        fault_slip_rate = np.mean(all_patches)
        self._mean_slip_rate = fault_slip_rate

    def get_unique_vertices(self):
        if self.patch_vertices is None:
            raise ValueError("Read in triangles first!")
        all_vertices = np.reshape(self.patch_vertices, (3 * len(self.patch_vertices), 3))
        unique_vertices = np.unique(all_vertices, axis=0)
        self._vertices = unique_vertices

    @property
    def triangles(self):
        if self._triangles is None:
            self.generate_triangles()
        return self._triangles

    @property
    def edge_lines(self):
        if self._edge_lines is None:
            self.generate_triangles()
        return self._edge_lines

    def generate_triangles(self):
        assert self.patch_outlines is not None, "Load patches first!"
        all_vertices = [patch.vertices for patch in self.patch_outlines]
        unique_vertices = np.unique(np.vstack(all_vertices), axis=0)
        self._vertices = unique_vertices

        triangle_ls = []
        line_ls = []
        for triangle in all_vertices:
            vertex_numbers = []
            for vertex in triangle:
                index = np.where((unique_vertices == vertex).all(axis=1))[0][0]
                vertex_numbers.append(index)
            triangle_ls.append(vertex_numbers)
            line_ls += [[vertex_numbers[0], vertex_numbers[1]],
                        [vertex_numbers[0], vertex_numbers[2]],
                        [vertex_numbers[1], vertex_numbers[2]]]
        self._triangles = np.array(triangle_ls)
        self._edge_lines = np.array(line_ls)

    def find_triangles_from_vertex_index(self, vertex_index: int):
        assert isinstance(vertex_index, int)
        assert 0 <= vertex_index < len(self.vertices)
        triangle_index_list = []
        for i, triangle in enumerate(self.triangles):
            if vertex_index in triangle:
                triangle_index_list.append(i)

        print(triangle_index_list)
        return triangle_index_list

    @classmethod
    def from_triangles(cls, triangles: Union[np.ndarray, list, tuple], segment_number: int = 0,
                       patch_numbers: Union[list, tuple, set, np.ndarray] = None, fault_name: str = None,
                       strike_slip: Union[int, float] = None, dip_slip: Union[int, float] = None,
                       rake: Union[int, float] = None, total_slip: np.ndarray = None, min_patch_area: float = 1.):
        """
        Create a segment from triangle vertices and (if appropriate) populate it with strike-slip/dip-slip values
        either specified separately or (if total_slip and rake are given) calculated from total slip + rake.
        :param segment_number:
        :param triangles:
        :param patch_numbers:
        :param fault_name:
        :param strike_slip:
        :param dip_slip:
        :param total_slip:
        :param rake:
        :return:
        """
        # Test shape of input array is appropriate
        triangle_array = np.array(triangles)
        assert triangle_array.shape[1] == 9, "Expecting 3d coordinates of 3 vertices each"
        # check no patches have 0 area
        triangle_verts = np.reshape(triangle_array, [len(triangle_array), 3, 3])
        for i, triangle in enumerate(triangle_verts):
            side1 = triangle[1] - triangle[0]
            side2 = triangle[1] - triangle[2]
            cross_prod = cross_3d(side1, side2)
            norm_cross = norm_3d(cross_prod)
            area = 0.5 * norm_cross
            if area < min_patch_area:
                np.delete(triangle_array, i, axis=0)
                if patch_numbers is not None:
                    np.delete(patch_numbers, i, axis=0)

        if patch_numbers is None:
            patch_numbers = np.arange(len(triangle_array))
        else:
            assert len(patch_numbers) == triangle_array.shape[0], "Need one patch for each triangle"

        # Create empty segment object
        fault = cls(patch_type="triangle", segment_number=segment_number, fault_name=fault_name)

        triangle_ls = []

        # Populate segment object
        for i, (patch_num, triangle) in enumerate(zip(patch_numbers, triangle_array)):
            triangle3 = triangle.reshape(3, 3)
            if total_slip is not None:
                assert rake is not None, "Specifying total slip requires rake to calculate strike-slip and dip-slip components"
                if strike_slip is not None:
                    print('Both total slip rate and strike slip rate specified '
                          '- strike slip and dip slip rates will be recalculated based on total slip and rake')

                patch = RsqSimTriangularPatch(fault, vertices=triangle3, patch_number=patch_num,
                                              strike_slip=strike_slip,
                                              dip_slip=dip_slip, rake=rake, total_slip=total_slip[i])
            else:
                patch = RsqSimTriangularPatch(fault, vertices=triangle3, patch_number=patch_num,
                                              strike_slip=strike_slip,
                                              dip_slip=dip_slip, rake=rake)
            triangle_ls.append(patch)

        fault.patch_outlines = triangle_ls
        fault.patch_numbers = np.array([patch.patch_number for patch in triangle_ls])
        fault.patch_dic = {p_num: patch for p_num, patch in zip(fault.patch_numbers, fault.patch_outlines)}

        return fault

    @classmethod
    def from_tsurface(cls, tsurface_file: str, segment_number: int = 0,
                      patch_numbers: Union[list, tuple, set, np.ndarray] = None, fault_name: str = None,
                      strike_slip: Union[int, float] = None, dip_slip: Union[int, float] = None):
        assert os.path.exists(tsurface_file)
        tsurface_mesh = tsurf(tsurface_file)

        fault = cls.from_triangles(tsurface_mesh.triangles, segment_number=segment_number, patch_numbers=patch_numbers,
                                   fault_name=fault_name, strike_slip=strike_slip, dip_slip=dip_slip)
        return fault

    @classmethod
    def from_dxf(cls, dxf_file: str, segment_number: int = 0,
                 patch_numbers: Union[list, tuple, set, np.ndarray] = None, fault_name: str = None,
                 strike_slip: Union[int, float] = None, dip_slip: Union[int, float] = None):
        triangles, boundary = read_dxf(dxf_file)
        segment = cls.from_triangles(triangles, segment_number=segment_number, patch_numbers=patch_numbers,
                                     fault_name=fault_name, strike_slip=strike_slip, dip_slip=dip_slip)
        segment.boundary = boundary

        return segment

    @classmethod
    def from_pandas(cls, dataframe: pd.DataFrame, segment_number: int,
                    patch_numbers: Union[list, tuple, set, np.ndarray], fault_name: str = None,
                    strike_slip: Union[int, float] = None, dip_slip: Union[int, float] = None, read_rake: bool = True,
                    read_slip_rate: bool = True, transform_from_utm: bool = False):

        triangles = dataframe.iloc[:, :9].to_numpy()
        if transform_from_utm:
            reshaped_array = triangles.reshape((len(triangles) * 3), 3)
            transformed_array = transformer_utm2nztm.transform(reshaped_array[:, 0], reshaped_array[:, 1],
                                                               reshaped_array[:, 2])
            reordered_array = np.vstack(transformed_array).T
            triangles_nztm = reordered_array.reshape((len(triangles), 9))

        else:
            triangles_nztm = triangles

        # Create empty segment object
        fault = cls(patch_type="triangle", segment_number=segment_number, fault_name=fault_name)

        triangle_ls = []

        if read_slip_rate:
            assert "slip_rate" in dataframe.columns, "Cannot read slip rate"
            slip_rate = dataframe.slip_rate.to_numpy()
        else:
            # set slip rate to 1 for calculating tsunami green functions
            slip_rate = 1

        if read_rake:
            assert "rake" in dataframe.columns, "Cannot read rake"
            assert all([a is None for a in (dip_slip, strike_slip)]), "Either read_rake or specify ds and ss, not both!"
            rake = dataframe.rake.to_numpy()
            rake_dic = {r: (np.cos(np.radians(r)), np.sin(np.radians(r))) for r in np.unique(rake)}
            assert len(rake) == len(triangles_nztm)
        else:
            rake = np.zeros((len(triangles_nztm),))

        # Populate segment object
        for i, (patch_num, triangle) in enumerate(zip(patch_numbers, triangles_nztm)):
            triangle3 = triangle.reshape(3, 3)
            if read_rake:
                if read_slip_rate:
                    strike_slip = rake_dic[rake[i]][0] * slip_rate[i]
                    dip_slip = rake_dic[rake[i]][1] * slip_rate[i]
                else:
                    strike_slip = rake_dic[rake[i]][0]
                    dip_slip = rake_dic[rake[i]][1]

            patch = RsqSimTriangularPatch(fault, vertices=triangle3, patch_number=patch_num,
                                          strike_slip=strike_slip,
                                          dip_slip=dip_slip, total_slip=slip_rate[i], rake=rake[i])
            triangle_ls.append(patch)

        fault.patch_outlines = triangle_ls
        fault.patch_numbers = patch_numbers
        fault.patch_dic = {p_num: patch for p_num, patch in zip(fault.patch_numbers, fault.patch_outlines)}

        return fault

    @classmethod
    def from_pickle(cls, dataframe: pd.DataFrame, segment_number: int,
                    patch_numbers: Union[list, tuple, set, np.ndarray], fault_name: str = None):
        patches = dataframe.to_numpy()

        # Create empty segment object
        fault = cls(patch_type="triangle", segment_number=segment_number, fault_name=fault_name)

        triangle_ls = []
        # Populate segment object
        for i, patch_num in enumerate(patch_numbers):
            patch_data = patches[i]
            patch = RsqSimTriangularPatch(fault, vertices=patch_data[0], patch_number=patch_num,
                                          strike_slip=patch_data[8],
                                          dip_slip=patch_data[7],
                                          patch_data=patch_data[1:7])
            triangle_ls.append(patch)

        fault.patch_outlines = triangle_ls
        fault.patch_numbers = patch_numbers
        fault.patch_dic = {p_num: patch for p_num, patch in zip(fault.patch_numbers, fault.patch_outlines)}

        return fault

    @classmethod
    def from_stl(cls, stl_file: str, segment_number: int = 0,
                 patch_numbers: Union[list, tuple, set, np.ndarray] = None, fault_name: str = None,
                 strike_slip: Union[int, float] = None, dip_slip: Union[int, float] = None,
                 rake: Union[int, float] = None, total_slip: np.ndarray = None):

        triangles = read_stl(stl_file)
        return cls.from_triangles(triangles, segment_number=segment_number, patch_numbers=patch_numbers,
                                  fault_name=fault_name, strike_slip=strike_slip, dip_slip=dip_slip, rake=rake)

    @property
    def adjacency_map(self):
        if self._adjacency_map is None:
            self.build_adjacency_map()
        return self._adjacency_map

    def build_adjacency_map(self):
        """
        For each triangle vertex, find the indices of the adjacent triangles.
        This function overwrites that from the parent class TriangularPatches.

        :Kwargs:
            * verbose       : Speak to me

        :Returns:
            * None
        """

        self._adjacency_map = []

        # Cache the vertices and faces arrays

        # First find adjacent triangles for all triangles
        # Currently any triangle with a edge, could be a common vertex instead.
        for vertex_numbers in self.triangles:
            adjacent_triangles = []
            for j, triangle in enumerate(self.triangles):
                common_vertices = [a for a in vertex_numbers if a in triangle]
                if len(common_vertices) == 2:
                    adjacent_triangles.append(j)
            self._adjacency_map.append(adjacent_triangles)

    def build_laplacian_matrix(self):

        """
        Build a discrete Laplacian smoothing matrix.

        :Args:
            * verbose       : if True, displays stuff.
            * method        : Method to estimate the Laplacian operator

                - 'count'   : The diagonal is 2-times the number of surrounding nodes. Off diagonals are -2/(number of surrounding nodes) for the surrounding nodes, 0 otherwise.
                - 'distance': Computes the scale-dependent operator based on Desbrun et al 1999. (Mathieu Desbrun, Mark Meyer, Peter Schr\"oder, and Alan Barr, 1999. Implicit Fairing of Irregular Meshes using Diffusion and Curvature Flow, Proceedings of SIGGRAPH).

            * irregular     : Not used, here for consistency purposes

        :Returns:
            * Laplacian     : 2D array
        """

        # Build the tent adjacency map
        if self.adjacency_map is None:
            self.build_adjacency_map()

        # Get the vertices

        # Allocate an array
        laplacian_matrix = np.zeros((len(self.patch_numbers), len(self.patch_numbers)))

        # Normalize the distances
        all_distances = []
        for i, (patch, adjacents) in enumerate(zip(self.patch_outlines, self.adjacency_map)):
            patch_centre = patch.centre
            distances = np.array([np.linalg.norm(self.patch_outlines[a].centre - patch_centre) for a in adjacents])
            all_distances.append(distances)
        normalizer = np.max([np.max(d) for d in all_distances])

        # Iterate over the vertices
        for i, (adjacents, distances) in enumerate(zip(self.adjacency_map, all_distances)):
            # Distance-based
            distances_normalized = distances / normalizer
            e = np.sum(distances_normalized)
            laplacian_matrix[i, i] = float(len(adjacents)) * 2. / e * np.sum(1. / distances_normalized)
            laplacian_matrix[i, adjacents] = -2. / e * 1. / distances_normalized

        self._laplacian = np.hstack((laplacian_matrix, laplacian_matrix))

    @property
    def laplacian(self):
        if self._laplacian is None:
            self.build_laplacian_matrix()
        return self._laplacian

    def find_top_vertex_indices(self, depth_tolerance: Union[float, int] = 100):
        top_vertex_depth = max(self.vertices[:, -1])
        shallow_indices = np.where(self.vertices[:, -1] >= top_vertex_depth - depth_tolerance)[0]
        return shallow_indices

    def find_top_vertices(self, depth_tolerance: Union[float, int] = 100):
        shallow_indices = self.find_top_vertex_indices(depth_tolerance)
        return self.vertices[shallow_indices]

    def find_top_edges(self, depth_tolerance: Union[float, int] = 100):
        shallow_indices = self.find_top_vertex_indices(depth_tolerance)
        top_edges = self.edge_lines[np.all(np.isin(self.edge_lines, shallow_indices), axis=1)]
        return top_edges

    @property
    def trace(self):
        if self._trace is None:
            top_edges = self.find_top_edges()
            line_list = []
            for edge in top_edges:
                v1 = self.vertices[edge[0]]
                v2 = self.vertices[edge[1]]
                line = LineString([v1[:-1], v2[:-1]])
                line_list.append(line)
            return linemerge(line_list)
        else:
            return self._trace

    @trace.setter
    def trace(self, trace: LineString):
        assert isinstance(trace, LineString)
        self._trace = trace

    @property
    def fault_outline(self):
        multip = MultiPolygon(patch.as_polygon() for patch in self.patch_outlines)
        return unary_union(list(multip.geoms))

    def plot_2d(self, ax: plt.Axes):
        ax.triplot(self.vertices[:, 0], self.vertices[:, 1], self.triangles)

    def to_mesh(self, write_slip: bool = False):
        mesh = meshio.Mesh(points=self.vertices, cells=[("triangle", self.triangles)])
        if write_slip:
            mesh.cell_data["slip"] = np.array([patch.total_slip for patch in self.patch_outlines])
        return mesh

    def to_stl(self, stl_name: str):
        mesh = self.to_mesh()
        mesh.write(stl_name, file_format="stl")

    def to_vtk(self, vtk_name: str, write_slip: bool = False):
        mesh = self.to_mesh(write_slip=write_slip)
        mesh.write(vtk_name, file_format="vtk")

    @property
    def dip_slip(self):
        return np.array([patch.dip_slip for patch in self.patch_outlines]).flatten()

    @property
    def strike_slip(self):
        return np.array([patch.strike_slip for patch in self.patch_outlines]).flatten()

    @property
    def rake(self):
        return np.array([patch.rake for patch in self.patch_outlines]).flatten()

    @dip_slip.setter
    def dip_slip(self, ds_array: np.ndarray):
        assert len(ds_array) == len(self.patch_outlines)
        for patch, ds in zip(self.patch_outlines, ds_array):
            patch.dip_slip = ds

    @strike_slip.setter
    def strike_slip(self, ss_array: np.ndarray):
        assert len(ss_array) == len(self.patch_outlines)
        for patch, ss in zip(self.patch_outlines, ss_array):
            patch.strike_slip = ss

    def to_rsqsim_fault_file(self, flt_name):
        tris = pd.DataFrame(self.patch_triangle_rows)
        if self.rake is not None:
            rakes = pd.Series(self.rake)
        else:
            rakes = pd.Series(np.ones(self.dip_slip.shape) * 90.)
            print("Rake not set, writing out as 90")
        tris.loc[:, 9] = rakes
        # slip_rates = pd.Series(self.dip_slip * 1.e-3 / csts.seconds_per_year)
        total_slip = [np.linalg.norm([self.dip_slip[i], self.strike_slip[i]]) for i in range(len(self.dip_slip))]
        slip_rates = pd.Series([rate * 1.e-3 / csts.seconds_per_year for rate in total_slip])
        tris.loc[:, 10] = slip_rates
        segment_num = pd.Series(np.ones(self.dip_slip.shape) * self.segment_number, dtype=np.int)
        tris.loc[:, 11] = segment_num
        seg_names = pd.Series([self.name for i in range(len(self.patch_numbers))])
        tris.loc[:, 12] = seg_names

        tris.to_csv(flt_name, index=False, header=False, sep="\t", encoding='ascii')

    def to_rsqsim_fault_array(self):
        tris = pd.DataFrame(self.patch_triangle_rows)
        if self.rake is not None:
            rakes = pd.Series(self.rake)
        else:
            rakes = pd.Series(np.ones(self.dip_slip.shape) * 90.)
            print("Rake not set, writing out as 90")
        tris.loc[:, 9] = rakes
        total_slip = [np.linalg.norm([self.dip_slip[i], self.strike_slip[i]]) for i in range(len(self.dip_slip))]
        srs = [rate * 1.e-3 / csts.seconds_per_year for rate in total_slip]
        try:
            rates = [slip.item() for slip in srs]
        except AttributeError:
            rates = srs
        slip_rates = pd.Series(rates)

        if any([rate < 1.e-15 and rate > 0. for rate in slip_rates]):
            print("Non-zero slip rates less than 1e-15 - check your units (this function assumes mm/yr as input)")
        tris.loc[:, 10] = slip_rates
        segment_num = pd.Series(np.ones(self.dip_slip.shape) * self.segment_number, dtype=np.int)
        tris.loc[:, 11] = segment_num
        seg_names = pd.Series([self.name for i in range(len(self.patch_numbers))])
        tris.loc[:, 12] = seg_names

        return tris

    @property
    def dip_dir(self):
        if self._dip_dir is None:
            dip_dir = calculate_dip_direction(self.trace)
            dip_dir_vec = np.array([np.sin(np.radians(dip_dir)), np.cos(np.radians(dip_dir))])
            centre_point = self.trace.interpolate(self.trace.length)
            vertex_locations = self.vertices[:, :-1] - np.array([centre_point.x,
                                                                 centre_point.y])
            along_strike_vec = np.array([np.sin(np.radians(dip_dir - 90.)), np.cos(np.radians(dip_dir - 90.))])
            along_strike_dist = np.abs(np.dot(vertex_locations, along_strike_vec))
            relevant_vertices = vertex_locations[along_strike_dist < 5000.]
            distances = np.array([np.matmul(relevant_vertices, dip_dir_vec) for i in range(relevant_vertices.shape[0])])
            if len(distances[distances > 0]) > distances.size / 2.:
                self._dip_dir = dip_dir

            else:
                self._dip_dir = reverse_bearing(dip_dir)
        return self._dip_dir

    @property
    def dip_direction_vector(self):
        dip_dir_vec = np.array([np.sin(np.radians(self.dip_dir)), np.cos(np.radians(self.dip_dir)), 0.])
        return dip_dir_vec

    @property
    def strike_direction_vector(self):
        strike_dir = self.dip_dir - 90.
        strike_dir_vec = np.array([np.sin(np.radians(strike_dir)), np.cos(np.radians(strike_dir)), 0.])
        return strike_dir_vec

    def get_average_dip(self, approx_spacing: float = 5000.0):
        centre_points, width = optimize_point_spacing(self.trace, approx_spacing)
        centre_array = np.vstack([centre_point.coords for centre_point in centre_points])
        centre_array_3d = np.vstack([centre_array.T, np.zeros(centre_array.shape[0])]).T

        local_dips = []
        for centre in centre_array_3d:
            relevant_vertices = self.vertices[np.abs(np.dot(self.vertices - centre, self.strike_direction_vector)
                                                     < width / 2.)]
            horizontal_dists = np.dot(relevant_vertices - centre, self.dip_direction_vector)
            depths = np.abs(relevant_vertices[:, -1])
            local_dip = fit_2d_line(horizontal_dists, depths)
            local_dips.append(local_dip)

        return abs(np.median(local_dips))

    def discretize_rectangular_tiles(self, tile_size: float = 5000., interpolation_distance: float = 1000.):
        """
        Discretize the fault into rectangular tiles of the given size.
        :param tile_size: Size of the tiles in metres.
        :return: A list of rectangular tiles.
        """
        centre_points, width = optimize_point_spacing(self.trace, tile_size)
        centre_array = np.vstack([centre_point.coords for centre_point in centre_points])
        centre_array_3d = np.vstack([centre_array.T, np.zeros(centre_array.shape[0])]).T

        dip_angle = self.get_average_dip(tile_size)
        down_dip_vector = np.array([np.cos(np.radians(dip_angle)) * self.dip_direction_vector[0],
                                    np.cos(np.radians(dip_angle)) * self.dip_direction_vector[1],
                                    -1 * np.sin(np.radians(dip_angle))])
        plane_normal = np.array([np.sin(np.radians(dip_angle)) * self.dip_direction_vector[0],
                                 np.sin(np.radians(dip_angle)) * self.dip_direction_vector[1],
                                 np.cos(np.radians(dip_angle))])

        rotation_matrix = np.column_stack((down_dip_vector, self.strike_direction_vector, plane_normal))
        plane_origin = centre_array_3d[0]

        rotated_vertices = np.dot(rotation_matrix.T, (self.vertices - plane_origin).T).T
        # rotated_centre_array_3d = fault_global_to_local(centre_array_3d, rotation_matrix, plane_origin)
        rotated_centre_array_3d = np.dot(rotation_matrix.T, (centre_array_3d - plane_origin).T).T
        rotated_down_dip_vector = np.matmul(rotation_matrix.T, down_dip_vector)
        rotated_along_strike_vector = np.matmul(rotation_matrix.T, self.strike_direction_vector)
        centre_points_for_plane_fitting = []
        interpolation_widths = []
        for centre in rotated_centre_array_3d:
            relevant_vertices = rotated_vertices[np.abs(np.dot(rotated_vertices - centre, rotated_along_strike_vector))
                                                 < width / 2.]
            horizontal_dists = np.dot(relevant_vertices[:, :-1] - centre[:-1], rotated_down_dip_vector[:-1])
            depths = relevant_vertices[:, -1]

            start_across = 0.
            end_across = max(horizontal_dists)
            initial_spacing = np.arange(start_across, end_across, interpolation_distance)

            # Combine and sort distances along profiles (with z)
            across_vs_z = np.vstack((horizontal_dists, depths)).T
            sorted_coords = across_vs_z[across_vs_z[:, 0].argsort()]

            # Interpolate, then turn into shapely linestring
            interp_z = np.interp(initial_spacing, sorted_coords[:, 0], sorted_coords[:, 1])
            if len(interp_z) > 1:
                interp_line = LineString(np.vstack((initial_spacing, interp_z)).T)

                # Interpolate locations of profile centres
                interpolated_points, interp_width = optimize_point_spacing(interp_line, tile_size)
                interpolation_widths += [interp_width for i in range(len(interpolated_points))]

                # Turn coordinates of interpolated points back into arrays
                interpolated_x = np.array([point.x for point in interpolated_points])
                interpolated_z_values = np.array([point.y for point in interpolated_points])

                # Calculate local coordinates of tile centres
                point_xys = np.array([xi * rotated_down_dip_vector[:-1] + centre[:-1] for xi in interpolated_x])
                point_xyz = np.vstack((point_xys.T, interpolated_z_values)).T
                centre_points_for_plane_fitting.append(point_xyz)

        # Collate tile centres
        centre_points_for_plane_fitting = np.vstack(centre_points_for_plane_fitting)
        # Turn local coordinates back into global coordinates
        plane_fitting_centre_points_xyz = np.dot(rotation_matrix, centre_points_for_plane_fitting.T).T + plane_origin
        all_tile_ls = []
        for plane_fitting_centre, interp_width in zip(plane_fitting_centre_points_xyz, interpolation_widths):
            relative_positions = self.vertices - plane_fitting_centre
            along_dists = np.dot(relative_positions, self.strike_direction_vector)
            across_dists = np.dot(relative_positions, down_dip_vector)
            of_interest = (along_dists >= -1 * width / 2) * (along_dists <= width / 2.) * \
                          (across_dists >= -1 * interp_width / 2) * (across_dists <= interp_width / 2)
            relevant_vertices = self.vertices[of_interest]
            # Normal to plane
            normal_i, _ = fit_plane_to_points(relevant_vertices)

            # Make sure normal points up
            if normal_i[-1] < 0:
                normal_i *= -1

            # Calculate along-strike vector (left-hand-rule)
            strike_vector = np.cross(normal_i, np.array([0, 0, -1]))
            strike_vector[-1] = 0
            strike_vector /= np.linalg.norm(strike_vector)

            # Create down-dip vector
            down_dip_vector = np.cross(normal_i, strike_vector)
            if down_dip_vector[-1] > 0:
                down_dip_vector *= -1

            if np.linalg.norm(down_dip_vector[:-1]) > 1.e-15:
                dip = np.degrees(np.arctan(-1 * down_dip_vector[-1] / np.linalg.norm(down_dip_vector[:-1])))
            else:
                dip = 90.
            # dips.append(dip)

            poly_ls = []
            for i, j in zip([1, 1, -1, -1], [1, -1, -1, 1]):
                corner_i = plane_fitting_centre + (
                            i * strike_vector * width / 2 + j * down_dip_vector * interp_width / 2.)
                if corner_i[-1] > 0.:
                    corner_i[-1] = 0.
                poly_ls.append(corner_i)

            # top_depths.append(poly_ls[1][-1])
            # bottom_depths.append(poly_ls[0][-1])
            #
            # top_trace = LineString(poly_ls[1:-1])
            # top_traces.append(top_trace)

            all_tile_ls.append(np.array(poly_ls))

        return np.array(all_tile_ls)


class RsqSimFault:
    """
    The idea is to allow a fault to have one or more segments
    """

    def __init__(self, segments: Union[RsqSimSegment, List[RsqSimSegment]]):
        self._segments = None
        self._vertices = None

        if segments is not None:
            self.segments = segments

    @property
    def segments(self):
        return self._segments

    @segments.setter
    def segments(self, segments: Union[RsqSimSegment, List[RsqSimSegment]]):

        if isinstance(segments, RsqSimSegment):
            self._segments = [segments]
        else:
            assert isinstance(segments, Iterable), "Expected either one segment or a list of segments"
            assert all([isinstance(segment, RsqSimSegment) for segment in segments]), "Expected a list of segments"
            self._segments = list(segments)


class OpenQuakeSegment:
    def __init__(self, polygons: list):
        self._polygons = polygons
