"""Shared visual scene, independent of hardware transports."""

import socket


def check_port(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def create_scene(server, models):
    from viser.extras import ViserUrdf

    server.scene.set_up_direction("+z")
    server.initial_camera.position = (1.3, -1.7, 1.0)
    server.initial_camera.look_at = (0.32, 0.23, 0.42)
    server.initial_camera.fov = 0.85
    server.scene.add_grid("/ground", width=2, height=2)
    roots, visuals = {}, {}
    for side, urdf in models.items():
        roots[side] = server.scene.add_frame(
            f"/robot_{side}", show_axes=False, visible=side == "body"
        )
        visuals[side] = ViserUrdf(server, urdf, root_node_name=f"/robot_{side}")
    return roots, visuals
