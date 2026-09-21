"""Street-network extraction and routing for stylized market flyers."""

from dataclasses import dataclass
import math

import numpy as np
from scipy import ndimage, sparse
from scipy.sparse import csgraph
from scipy.spatial import cKDTree


class FlyerStreetError(ValueError):
    """Raised when a usable printed-street network cannot be built."""


@dataclass(frozen=True)
class StreetAccess:
    stop_xy: tuple[float, float]
    street_xy: tuple[float, float]
    node: int
    distance_px: float


@dataclass(frozen=True)
class RouteValidation:
    closed: bool
    unique_stop_count: int
    max_access_distance_px: float
    max_mask_distance_px: float


def _scaled(value, dpi):
    return max(1, int(round(value * dpi / 300)))


def _street_color_mask(image):
    rgb = np.asarray(image, dtype=np.int16)[..., :3]
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return (
        (red >= 125) & (red <= 235) &
        (green >= 155) & (green <= 248) &
        (blue >= 185) &
        (blue - red >= 18) &
        (blue - green >= 5)
    )


def _drop_small_components(mask, dpi):
    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
    if not count:
        return mask
    sizes = np.bincount(labels.ravel())
    minimum = _scaled(30, dpi)
    keep = sizes >= minimum
    keep[0] = False
    return keep[labels]


def extract_road_masks(image, bbox, dpi=300):
    """Return visible and gap-closed street masks inside ``bbox``."""
    visible = _street_color_mask(image)
    bounded = np.zeros_like(visible)
    x0, y0, x1, y1 = (int(v) for v in bbox)
    x0, x1 = max(0, x0), min(visible.shape[1], x1)
    y0, y1 = max(0, y0), min(visible.shape[0], y1)
    bounded[y0:y1, x0:x1] = True
    visible &= bounded
    visible = _drop_small_components(visible, dpi)

    gap = _scaled(24, dpi)
    horizontal = ndimage.binary_closing(
        visible, structure=np.ones((1, gap + 1), dtype=bool))
    vertical = ndimage.binary_closing(
        visible, structure=np.ones((gap + 1, 1), dtype=bool))
    routable = (visible | horizontal | vertical) & bounded
    routable = _drop_small_components(routable, dpi)
    return visible, routable


def morphological_skeleton(mask):
    """Topology-preserving Zhang-Suen thinning of a binary road mask."""
    skeleton = np.pad(
        np.asarray(mask, dtype=np.uint8), 1, mode="constant")

    def removable(first_pass):
        center = skeleton[1:-1, 1:-1]
        p2 = skeleton[:-2, 1:-1]
        p3 = skeleton[:-2, 2:]
        p4 = skeleton[1:-1, 2:]
        p5 = skeleton[2:, 2:]
        p6 = skeleton[2:, 1:-1]
        p7 = skeleton[2:, :-2]
        p8 = skeleton[1:-1, :-2]
        p9 = skeleton[:-2, :-2]
        neighbours = (p2, p3, p4, p5, p6, p7, p8, p9)
        count = sum(neighbours)
        transitions = sum(
            ((neighbours[i] == 0) & (neighbours[(i + 1) % 8] == 1))
            for i in range(8)
        )
        common = ((center == 1) & (count >= 2) & (count <= 6) &
                  (transitions == 1))
        if first_pass:
            return common & ((p2 * p4 * p6) == 0) & ((p4 * p6 * p8) == 0)
        return common & ((p2 * p4 * p8) == 0) & ((p2 * p6 * p8) == 0)

    changed = True
    while changed:
        changed = False
        for first_pass in (True, False):
            remove = removable(first_pass)
            if remove.any():
                skeleton[1:-1, 1:-1][remove] = 0
                changed = True
    return skeleton[1:-1, 1:-1].astype(bool)


@dataclass
class FlyerStreetGraph:
    road_mask: np.ndarray
    routing_mask: np.ndarray
    skeleton: np.ndarray
    node_xy: np.ndarray
    adjacency: sparse.csr_matrix
    pixel_to_node: np.ndarray
    dpi: int

    def __post_init__(self):
        self._node_tree = cKDTree(self.node_xy)

    @classmethod
    def from_image(cls, image, bbox, dpi=300):
        road_mask, routing_mask = extract_road_masks(image, bbox, dpi)
        skeleton = morphological_skeleton(routing_mask)
        return cls.from_skeleton(
            road_mask, skeleton, dpi=dpi, routing_mask=routing_mask)

    @classmethod
    def from_skeleton(cls, road_mask, skeleton, dpi=300,
                      routing_mask=None):
        road_mask = np.asarray(road_mask, dtype=bool)
        skeleton = np.asarray(skeleton, dtype=bool)
        ys, xs = np.where(skeleton)
        if not len(xs):
            raise FlyerStreetError("no printed street network detected")
        node_xy = np.column_stack((xs, ys)).astype(float)
        pixel_to_node = np.full(skeleton.shape, -1, dtype=np.int32)
        pixel_to_node[ys, xs] = np.arange(len(xs), dtype=np.int32)

        rows, cols, weights = [], [], []
        height, width = skeleton.shape
        for dx, dy in ((1, 0), (0, 1), (1, 1), (-1, 1)):
            shifted_y = ys + dy
            shifted_x = xs + dx
            valid = ((shifted_y >= 0) & (shifted_y < height) &
                     (shifted_x >= 0) & (shifted_x < width))
            source = pixel_to_node[ys[valid], xs[valid]]
            target = pixel_to_node[shifted_y[valid], shifted_x[valid]]
            linked = target >= 0
            source, target = source[linked], target[linked]
            weight = math.hypot(dx, dy)
            rows.extend(source.tolist())
            cols.extend(target.tolist())
            weights.extend([weight] * len(source))
            rows.extend(target.tolist())
            cols.extend(source.tolist())
            weights.extend([weight] * len(source))

        adjacency = sparse.csr_matrix(
            (weights, (rows, cols)), shape=(len(xs), len(xs)))
        return cls(
            road_mask=road_mask,
            routing_mask=(np.asarray(routing_mask, dtype=bool)
                          if routing_mask is not None else road_mask.copy()),
            skeleton=skeleton,
            node_xy=node_xy,
            adjacency=adjacency,
            pixel_to_node=pixel_to_node,
            dpi=dpi,
        )

    def nearest_node(self, xy, max_distance=None):
        distance, node = self._node_tree.query(np.asarray(xy, dtype=float))
        if max_distance is not None and distance > max_distance:
            raise FlyerStreetError(
                f"no printed street within {max_distance:g} pixels")
        return int(node)

    def connected(self, first_xy, second_xy):
        first = self.nearest_node(first_xy, max_distance=_scaled(8, self.dpi))
        second = self.nearest_node(second_xy, max_distance=_scaled(8, self.dpi))
        distances = csgraph.dijkstra(self.adjacency, indices=first)
        return bool(np.isfinite(distances[second]))

    def snap_stops(self, stops, max_distance_px):
        access = []
        for marker_number, stop in enumerate(stops, start=1):
            distance, node = self._node_tree.query(
                np.asarray(stop, dtype=float))
            if distance > max_distance_px:
                raise FlyerStreetError(
                    f"market marker {marker_number} is not connected to the "
                    "printed street network")
            street = self.node_xy[int(node)]
            access.append(StreetAccess(
                stop_xy=(float(stop[0]), float(stop[1])),
                street_xy=(float(street[0]), float(street[1])),
                node=int(node),
                distance_px=float(distance),
            ))
        if access:
            matrix = self.distance_matrix(access)
            disconnected = np.argwhere(~np.isfinite(matrix))
            if len(disconnected):
                marker_number = int(disconnected[0, 1]) + 1
                raise FlyerStreetError(
                    f"market marker {marker_number} is on a disconnected "
                    "printed street component")
        return access

    def distance_matrix(self, access):
        nodes = np.asarray([item.node for item in access], dtype=int)
        if not len(nodes):
            return np.empty((0, 0), dtype=float)
        unique_nodes, inverse = np.unique(nodes, return_inverse=True)
        distances = csgraph.dijkstra(
            self.adjacency, indices=unique_nodes, directed=False)
        return np.asarray(distances)[inverse][:, nodes]

    def route_geometry(self, order, access):
        if len(order) < 2:
            raise FlyerStreetError("a route needs at least two stop occurrences")
        source_nodes = np.asarray(
            sorted({access[index].node for index in order[:-1]}), dtype=int)
        _, predecessors = csgraph.dijkstra(
            self.adjacency, indices=source_nodes, directed=False,
            return_predecessors=True)
        source_rows = {
            node: row for row, node in enumerate(source_nodes.tolist())
        }
        route_nodes = []
        for first_index, second_index in zip(order, order[1:]):
            source = access[first_index].node
            target = access[second_index].node
            leg = [target]
            current = target
            while current != source:
                current = int(predecessors[source_rows[source], current])
                if current < 0:
                    raise FlyerStreetError(
                        f"no printed-street path between markers "
                        f"{first_index + 1} and {second_index + 1}")
                leg.append(current)
            leg.reverse()
            route_nodes.extend(leg if not route_nodes else leg[1:])
        return [tuple(self.node_xy[node]) for node in route_nodes]

    def validate_closed_route(self, order, access, route):
        closed = bool(
            len(order) > 1 and order[0] == order[-1] and route and
            np.allclose(route[0], route[-1]))
        stop_occurrences = order[:-1] if closed else order
        max_access = max(
            (access[index].distance_px for index in set(stop_occurrences)),
            default=0.0)
        off_mask = ndimage.distance_transform_edt(~self.routing_mask)
        max_mask_distance = 0.0
        for x, y in route:
            ix = int(np.clip(round(x), 0, off_mask.shape[1] - 1))
            iy = int(np.clip(round(y), 0, off_mask.shape[0] - 1))
            max_mask_distance = max(max_mask_distance, float(off_mask[iy, ix]))
        return RouteValidation(
            closed=closed,
            unique_stop_count=len(set(stop_occurrences)),
            max_access_distance_px=max_access,
            max_mask_distance_px=max_mask_distance,
        )
