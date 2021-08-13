import os
from collections import Iterable
from typing import Union, List

import numpy as np
import pandas as pd

from tde.tde import calc_tri_displacements
from triangular_faults.displacements import DisplacementArray
from matplotlib import pyplot as plt
from pyproj import Transformer

from rsqsim_api.io.read_utils import read_dxf, read_stl
from rsqsim_api.io.tsurf import tsurf
from rsqsim_api.fault.patch import RsqSimTriangularPatch, RsqSimGenericPatch


transformer_utm2nztm = Transformer.from_crs(32759, 2193, always_xy=True)


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
                       strike_slip: Union[int, float] = None, dip_slip: Union[int, float] = None):
        """
        Create a segment from triangle vertices and (if appropriate) populate it with strike-slip/dip-slip values
        :param segment_number:
        :param triangles:
        :param patch_numbers:
        :param fault_name:
        :param strike_slip:
        :param dip_slip:
        :return:
        """
        # Test shape of input array is appropriate
        triangle_array = np.array(triangles)
        assert triangle_array.shape[1] == 9, "Expecting 3d coordinates of 3 vertices each"
        if patch_numbers is None:
            patch_numbers = np.arange(len(triangle_array))
        else:
            assert len(patch_numbers) == triangle_array.shape[0], "Need one patch for each triangle"

        # Create empty segment object
        fault = cls(patch_type="triangle", segment_number=segment_number, fault_name=fault_name)

        triangle_ls = []

        # Populate segment object
        for patch_num, triangle in zip(patch_numbers, triangle_array):
            triangle3 = triangle.reshape(3, 3)
            patch = RsqSimTriangularPatch(fault, vertices=triangle3, patch_number=patch_num, strike_slip=strike_slip,
                                          dip_slip=dip_slip)
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
                    normalize_slip: Union[float, int] = 1, transform_from_utm: bool = False):

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

        if read_rake:
            assert "rake" in dataframe.columns, "Cannot read rake"
            assert all([a is None for a in (dip_slip, strike_slip)]), "Either read_rake or specify ds and ss, not both!"
            rake = dataframe.rake.to_numpy()
            rake_dic = {r: (np.cos(np.radians(r)) * normalize_slip, np.sin(np.radians(r)) * normalize_slip) for r in np.unique(rake)}
            assert len(rake) == len(triangles_nztm)
        else:
            rake = np.zeros((len(triangles_nztm),))

        # Populate segment object
        for i, (patch_num, triangle) in enumerate(zip(patch_numbers, triangles_nztm)):
            triangle3 = triangle.reshape(3, 3)
            if read_rake:
                strike_slip = rake_dic[rake[i]][0]
                dip_slip = rake_dic[rake[i]][1]
            patch = RsqSimTriangularPatch(fault, vertices=triangle3, patch_number=patch_num,
                                          strike_slip=strike_slip,
                                          dip_slip=dip_slip)
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
                 strike_slip: Union[int, float] = None, dip_slip: Union[int, float] = None):

        triangles = read_stl(stl_file)
        return cls.from_triangles(triangles, segment_number=segment_number, patch_numbers=patch_numbers,
                                  fault_name=fault_name, strike_slip=strike_slip, dip_slip=dip_slip)



    def collect_greens_function(self, sites_x: Union[list, tuple, np.ndarray], sites_y: Union[list, tuple, np.ndarray],
                                sites_z: Union[list, tuple, np.ndarray] = None, strike_slip: Union[int, float] = 1,
                                dip_slip: Union[int, float] = 1, poisson_ratio: Union[int, float] = 0.25,
                                tensional_slip: Union[int, float] = 0):
        """

        :param sites_x:
        :param sites_y:
        :param sites_z:
        :param strike_slip:
        :param dip_slip:
        :param poisson_ratio:
        :param tensional_slip:
        :return:
        """
        x_array = np.array(sites_x)
        y_array = np.array(sites_y)
        if sites_z is None:
            z_array = np.zeros(x_array.shape)
        else:
            z_array = np.array(sites_z)

        x_vertices, y_vertices, z_vertices = [[vertices.T[i] for vertices in self.patch_vertices] for i in range(3)]

        ds_gf_dic = {}
        ss_gf_dic = {}
        for patch_number, xv, yv, zv in zip(self.patch_numbers, x_vertices, y_vertices, z_vertices):
            ds_gf_dic[patch_number] = calc_tri_displacements(x_array, y_array, z_array, xv, yv, -1. * zv,
                                                             poisson_ratio, 0., tensional_slip, dip_slip)
            ss_gf_dic[patch_number] = calc_tri_displacements(x_array, y_array, z_array, xv, yv, -1. * zv,
                                                             poisson_ratio, strike_slip, tensional_slip, 0.)

        self.ds_gf = ds_gf_dic
        self.ss_gf = ss_gf_dic

    def gf_design_matrix(self, displacements: DisplacementArray):
        """
        Maybe this should be stored on the DisplacementArray object?
        :param displacements:
        :return:
        """
        assert isinstance(displacements, DisplacementArray), "Expecting DisplacementArray object"
        if any([a is None for a in (self.ds_gf, self.ss_gf)]):
            self.collect_greens_function(displacements.x, displacements.y, displacements.z)
        design_matrix_ls = []
        d_list = []
        w_list = []
        for component, key in zip([displacements.e, displacements.n, displacements.v], ["x", "y", "z"]):
            if component is not None:
                for site in range(len(component)):
                    component_ds = [self.ds_gf[i][key][site] for i in self.patch_numbers]
                    component_ss = [self.ss_gf[i][key][site] for i in self.patch_numbers]
                    component_combined = component_ds + component_ss
                    design_matrix_ls.append(component_combined)
                d_list.append(component)

        design_matrix_array = np.array(design_matrix_ls)
        d_array = np.hstack([a for a in d_list])

        return design_matrix_array, d_array

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

    def find_top_patches(self, depth_tolerance: Union[float, int] = 100):
        top_vertex_depth = max(self.vertices[:, -1])
        shallow_indices = np.where(self.vertices[:, -1] >= top_vertex_depth - depth_tolerance)[0]
        return shallow_indices

    def plot_2d(self, ax: plt.Axes):
        ax.triplot(self.vertices[:, 0], self.vertices[:, 1], self.triangles)


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