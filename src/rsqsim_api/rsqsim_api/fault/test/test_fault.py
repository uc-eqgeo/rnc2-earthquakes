import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from rsqsim_api.fault.segment import RsqSimTriangularPatch, RsqSimSegment
from rsqsim_api.fault.multifault import RsqSimMultiFault
test_vertices = np.array([[0., 0., 0.],
                          [1., 1., 0.],
                          [0., 1., -1]])

fault_names = pd.Series(["test"])

column_names = ["x1", "y1", "z1", "x2", "y2", "z2", "x3", "y3", "z3", "rake",
                "slip_rate", "fault_num", "bruce_name"]
faults_in = pd.DataFrame([[0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, -1.0, 180.0, 0, 0, 0]], columns=column_names)


class TestMultiFault(unittest.TestCase):
    @patch('rsqsim_api.fault.multifault.pd.read_csv')
    @patch('rsqsim_api.fault.multifault.all')
    def setUp(self, mock_all, mock_read_csv):
        mock_read_csv.side_effect = [fault_names, faults_in]
        mock_all.return_value = True
        self.multifault = RsqSimMultiFault.read_fault_file_bruce("path/to/fault", "path/to/names")

    def test_return_faults(self):
        segments = self.multifault.faults
        self.assertTrue(len(segments) == 1)
        self.assertIsInstance(segments[0], RsqSimSegment)

    def test_return_names(self):
        names = self.multifault.names
        self.assertTrue(len(names) == 1)
        self.assertEqual(names[0], "test")

    def test_return_name_dic(self):
        self.assertDictEqual(self.multifault.name_dic, {'test': self.multifault.faults[0]})

    def test_return_bounds(self):
        np.testing.assert_array_equal(self.multifault.bounds, np.array([0, 0, 1, 1]))

    def test_return_patch_dic(self):
        self.assertIsInstance(self.multifault.patch_dic[0], RsqSimTriangularPatch)

    def test_return_faults_with_patches(self):
        faults_with_patches = self.multifault.faults_with_patches
        self.assertIsInstance(faults_with_patches[0], RsqSimSegment)
        self.assertEqual(faults_with_patches[0], self.multifault.faults[0])

    def test_filter_faults_by_patch_numbers(self):
        patch = self.multifault.filter_faults_by_patch_numbers(0)
        self.assertIsInstance(patch, RsqSimTriangularPatch)
        self.assertEqual(patch.patch_number, 0)


class TestSegment(unittest.TestCase):
    def setUp(self) -> None:
        self.fault = RsqSimSegment.from_pandas(faults_in, 0, [0], "test")

    def test_return_vertices(self):
        np.testing.assert_array_equal(np.sort(self.fault.vertices.flat), np.sort(test_vertices.flat))

    def test_return_bounds(self):
        np.testing.assert_array_equal(self.fault.bounds, np.array([0, 0, 1, 1]))

    def test_return_patch_outlines(self):
        patch_outlines = self.fault.patch_outlines
        self.assertTrue(len(patch_outlines) == 1)
        self.assertIsInstance(patch_outlines[0], RsqSimTriangularPatch)

    def test_return_patch_numbers(self):
        patch_numbers = self.fault.patch_numbers
        self.assertTrue(len(patch_numbers) == 1)
        self.assertEqual(patch_numbers[0], 0)

    def test_return_patch_dic(self):
        self.assertIsInstance(self.fault.patch_dic[0], RsqSimTriangularPatch)

    def test_return_patch_vertices(self):
        patch_vertices = self.fault.patch_vertices
        self.assertTrue(len(patch_vertices) == 1)
        np.testing.assert_array_equal(np.sort(patch_vertices[0].flat), np.sort(test_vertices.flat))

    def test_return_edge_lines(self):
        np.testing.assert_array_equal(self.fault.edge_lines, [[0, 2], [0, 1], [2, 1]])

    def test_return_triangles(self):
        np.testing.assert_array_equal(self.fault.triangles, [[0, 2, 1]])

    def test_dip_slip(self):
        patch = self.fault.patch_dic[0]
        np.testing.assert_almost_equal(patch.dip_slip, 0)

    def test_strike_slip(self):
        patch = self.fault.patch_dic[0]
        np.testing.assert_almost_equal(patch.strike_slip, -1)

    def test_total_slip(self):
        patch = self.fault.patch_dic[0]
        np.testing.assert_almost_equal(patch.total_slip, 1)

    def test_find_triangles_from_vertex_index(self):
        triangle_index = self.fault.find_triangles_from_vertex_index(0)
        self.assertTrue(len(triangle_index) == 1)
        self.assertEqual(triangle_index[0], 0)
        self.assertIn(0, self.fault.triangles[triangle_index])


class TestTriangularPatch(unittest.TestCase):
    def setUp(self) -> None:
        self.segment = RsqSimSegment(0)
        self.triangle = RsqSimTriangularPatch(segment=self.segment, vertices=test_vertices)

    def test_return_vertices_shape(self):
        self.assertSequenceEqual(self.triangle.vertices.shape, test_vertices.shape)

    def test_return_vertices(self):
        np.testing.assert_array_almost_equal(self.triangle.vertices, test_vertices)

    def test_normal_vector(self):
        np.testing.assert_array_almost_equal(self.triangle.normal_vector, np.array([-0.57735, 0.57735, 0.57735]))

    def test_down_dip_vector(self):
        np.testing.assert_array_almost_equal(self.triangle.down_dip_vector, np.array([-0.301511, 0.301511, -0.904534]))

    def test_dip_magnitude(self):
        np.testing.assert_almost_equal(self.triangle.dip, 64.7605981)

    def test_along_strike_vector(self):
        np.testing.assert_array_almost_equal(self.triangle.along_strike_vector, np.array([-0.696311, -0.696311, 0.]))

    def test_centre(self):
        np.testing.assert_array_almost_equal(self.triangle.centre, np.array([0.333333, 0.6666667, -0.3333333]))

    def test_area(self):
        np.testing.assert_almost_equal(self.triangle.area, 0.8660254)

    def test_strike(self):
        self.assertEqual(self.triangle.strike, 225.0)
