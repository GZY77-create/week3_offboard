#!/usr/bin/env python3
"""PX4/MAVROS Offboard square mission for Gazebo SITL."""

import math
import threading

import rospy
from geometry_msgs.msg import PoseStamped, TwistStamped
from mavros_msgs.msg import ExtendedState, State
from mavros_msgs.srv import CommandBool, SetMode


class MissionError(RuntimeError):
    """Raised when continuing the flight would be unsafe."""


class OffboardMission:
    def __init__(self):
        self.state = State()
        self.extended_state = ExtendedState()
        self.pose = None
        self.velocity = None
        self.goal = PoseStamped()
        self.goal.header.frame_id = "map"
        self._lock = threading.Lock()
        self._shutdown_started = False

        self.rate_hz = float(rospy.get_param("~rate", 20.0))
        self.altitude = float(rospy.get_param("~altitude", 2.0))
        self.side_length = float(rospy.get_param("~side_length", 2.0))
        self.hover_seconds = float(rospy.get_param("~hover_seconds", 5.0))
        self.corner_hold_seconds = float(rospy.get_param("~corner_hold_seconds", 2.0))
        self.position_tolerance = float(rospy.get_param("~position_tolerance", 0.25))
        self.speed_tolerance = float(rospy.get_param("~speed_tolerance", 0.20))
        self.settle_seconds = float(rospy.get_param("~settle_seconds", 0.50))
        self.max_position_error = float(rospy.get_param("~max_position_error", 5.0))
        self.waypoint_timeout = float(rospy.get_param("~waypoint_timeout", 30.0))

        if self.max_position_error <= self.side_length:
            raise MissionError(
                "max_position_error ({:.2f} m) must exceed side_length "
                "({:.2f} m)".format(self.max_position_error, self.side_length)
            )

        rospy.Subscriber("/mavros/state", State, self._state_cb, queue_size=10)
        rospy.Subscriber(
            "/mavros/extended_state",
            ExtendedState,
            self._extended_state_cb,
            queue_size=10,
        )
        rospy.Subscriber(
            "/mavros/local_position/pose", PoseStamped, self._pose_cb, queue_size=10
        )
        rospy.Subscriber(
            "/mavros/local_position/velocity_local",
            TwistStamped,
            self._velocity_cb,
            queue_size=10,
        )
        self.setpoint_pub = rospy.Publisher(
            "/mavros/setpoint_position/local", PoseStamped, queue_size=20
        )

        rospy.wait_for_service("/mavros/cmd/arming", timeout=15.0)
        rospy.wait_for_service("/mavros/set_mode", timeout=15.0)
        self.arm_client = rospy.ServiceProxy("/mavros/cmd/arming", CommandBool)
        self.mode_client = rospy.ServiceProxy("/mavros/set_mode", SetMode)
        rospy.on_shutdown(self._on_shutdown)

    def _state_cb(self, msg):
        with self._lock:
            self.state = msg

    def _pose_cb(self, msg):
        with self._lock:
            self.pose = msg

    def _velocity_cb(self, msg):
        with self._lock:
            self.velocity = msg

    def _extended_state_cb(self, msg):
        with self._lock:
            self.extended_state = msg

    def _snapshot(self):
        with self._lock:
            return self.state, self.pose, self.velocity

    @staticmethod
    def _speed(velocity):
        linear = velocity.twist.linear
        return math.sqrt(linear.x * linear.x + linear.y * linear.y + linear.z * linear.z)

    def _wait_for_landing(self, timeout=60.0):
        """Wait for PX4 to report touchdown before requesting disarm."""
        deadline = rospy.Time.now() + rospy.Duration(timeout)
        rate = rospy.Rate(self.rate_hz)
        while not rospy.is_shutdown() and rospy.Time.now() < deadline:
            with self._lock:
                state = self.state
                landed_state = self.extended_state.landed_state
            if not state.connected:
                raise MissionError("FCU disconnected during landing")
            if not state.armed:
                rospy.loginfo("Landing complete: vehicle auto-disarmed")
                return
            if landed_state == ExtendedState.LANDED_STATE_ON_GROUND:
                rospy.loginfo("Touchdown confirmed; requesting disarm")
                self._request_arm(False, timeout=15.0)
                return
            rospy.loginfo_throttle(2.0, "AUTO.LAND: waiting for touchdown")
            rate.sleep()
        raise MissionError("Timed out waiting for touchdown")

    def _set_goal(self, x, y, z):
        self.goal.pose.position.x = x
        self.goal.pose.position.y = y
        self.goal.pose.position.z = z
        self.goal.pose.orientation.w = 1.0

    def _publish_goal(self):
        self.goal.header.stamp = rospy.Time.now()
        self.setpoint_pub.publish(self.goal)

    def _distance_to_goal(self, pose):
        dx = pose.pose.position.x - self.goal.pose.position.x
        dy = pose.pose.position.y - self.goal.pose.position.y
        dz = pose.pose.position.z - self.goal.pose.position.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def _wait_for_vehicle(self):
        rate = rospy.Rate(self.rate_hz)
        while not rospy.is_shutdown():
            state, pose, _ = self._snapshot()
            if state.connected and pose is not None:
                return pose
            rospy.loginfo_throttle(2.0, "Waiting for FCU connection and local pose")
            rate.sleep()
        raise MissionError("ROS shutdown while waiting for vehicle")

    def _stream_setpoints(self, seconds):
        rate = rospy.Rate(self.rate_hz)
        end = rospy.Time.now() + rospy.Duration(seconds)
        while not rospy.is_shutdown() and rospy.Time.now() < end:
            self._publish_goal()
            rate.sleep()

    def _request_mode(self, mode, timeout=30.0):
        deadline = rospy.Time.now() + rospy.Duration(timeout)
        rate = rospy.Rate(self.rate_hz)
        last_request = rospy.Time(0)
        while not rospy.is_shutdown() and rospy.Time.now() < deadline:
            state, _, _ = self._snapshot()
            if not state.connected:
                raise MissionError("FCU disconnected; refusing mode change")
            if state.mode == mode:
                rospy.loginfo("Mode confirmed: %s", mode)
                return
            if (rospy.Time.now() - last_request).to_sec() >= 2.0:
                try:
                    response = self.mode_client(base_mode=0, custom_mode=mode)
                    rospy.loginfo("Mode request %s: sent=%s", mode, response.mode_sent)
                except rospy.ServiceException as exc:
                    rospy.logwarn("Mode request failed, retrying: %s", exc)
                last_request = rospy.Time.now()
            self._publish_goal()
            rate.sleep()
        raise MissionError("Timed out switching to {}".format(mode))

    def _request_arm(self, arm, timeout=30.0):
        deadline = rospy.Time.now() + rospy.Duration(timeout)
        rate = rospy.Rate(self.rate_hz)
        last_request = rospy.Time(0)
        while not rospy.is_shutdown() and rospy.Time.now() < deadline:
            state, _, _ = self._snapshot()
            if arm and not state.connected:
                raise MissionError("FCU disconnected; refusing to arm")
            if state.armed == arm:
                rospy.loginfo("Armed state confirmed: %s", arm)
                return
            if (rospy.Time.now() - last_request).to_sec() >= 2.0:
                try:
                    response = self.arm_client(value=arm)
                    rospy.loginfo("Arm request %s: success=%s", arm, response.success)
                except rospy.ServiceException as exc:
                    rospy.logwarn("Arm request failed, retrying: %s", exc)
                last_request = rospy.Time.now()
            self._publish_goal()
            rate.sleep()
        raise MissionError("Timed out setting armed={}".format(arm))

    def _fly_to(self, name, x, y, z, hold_seconds=0.0):
        self._set_goal(x, y, z)
        rospy.loginfo("Target %-12s x=%.2f y=%.2f z=%.2f", name, x, y, z)
        deadline = rospy.Time.now() + rospy.Duration(self.waypoint_timeout)
        arrived_at = None
        rate = rospy.Rate(self.rate_hz)
        while not rospy.is_shutdown() and rospy.Time.now() < deadline:
            state, pose, velocity = self._snapshot()
            if not state.connected:
                raise MissionError("FCU disconnected during flight")
            if state.mode != "OFFBOARD":
                raise MissionError("Vehicle left OFFBOARD mode: {}".format(state.mode))
            if pose is None or velocity is None:
                rate.sleep()
                continue
            error = self._distance_to_goal(pose)
            speed = self._speed(velocity)
            if error > self.max_position_error:
                self._set_goal(
                    pose.pose.position.x,
                    pose.pose.position.y,
                    pose.pose.position.z,
                )
                self._stream_setpoints(3.0)
                raise MissionError(
                    "Position error {:.2f} m exceeded limit; holding".format(error)
                )
            settled = (
                error <= self.position_tolerance
                and speed <= self.speed_tolerance
            )
            if settled:
                if arrived_at is None:
                    arrived_at = rospy.Time.now()
                    rospy.loginfo(
                        "Settling at %s (error %.2f m, speed %.2f m/s)",
                        name,
                        error,
                        speed,
                    )
                required_stable_time = max(hold_seconds, self.settle_seconds)
                if (rospy.Time.now() - arrived_at).to_sec() >= required_stable_time:
                    rospy.loginfo(
                        "Reached %s (error %.2f m, speed %.2f m/s)",
                        name,
                        error,
                        speed,
                    )
                    return
            else:
                arrived_at = None
                rospy.loginfo_throttle(
                    1.0,
                    "Approaching %s: error %.2f m, speed %.2f m/s",
                    name,
                    error,
                    speed,
                )
            self._publish_goal()
            rate.sleep()
        raise MissionError("Timed out reaching {}".format(name))

    def run(self):
        initial = self._wait_for_vehicle()
        x0 = initial.pose.position.x
        y0 = initial.pose.position.y
        z_takeoff = initial.pose.position.z + self.altitude

        self._set_goal(x0, y0, z_takeoff)
        rospy.loginfo("Pre-streaming setpoints before OFFBOARD")
        self._stream_setpoints(5.0)
        self._request_mode("OFFBOARD")
        self._request_arm(True)

        self._fly_to("takeoff", x0, y0, z_takeoff, self.hover_seconds)
        square = [
            ("corner_1", x0 + self.side_length, y0),
            ("corner_2", x0 + self.side_length, y0 + self.side_length),
            ("corner_3", x0, y0 + self.side_length),
            ("corner_4", x0, y0),
        ]
        for name, x, y in square:
            self._fly_to(name, x, y, z_takeoff, self.corner_hold_seconds)

        rospy.loginfo("Square complete; requesting AUTO.LAND at start point")
        self._request_mode("AUTO.LAND")
        self._wait_for_landing(timeout=60.0)
        rospy.loginfo("Mission complete: landed and disarmed")

    def abort_safely(self, reason):
        rospy.logerr("Mission aborted: %s", reason)
        state, pose, _ = self._snapshot()
        if pose is not None:
            self._set_goal(
                pose.pose.position.x,
                pose.pose.position.y,
                pose.pose.position.z,
            )
            self._stream_setpoints(2.0)
        if state.connected and state.armed:
            try:
                self.mode_client(base_mode=0, custom_mode="AUTO.LAND")
                rospy.logwarn("Safety action: requested AUTO.LAND")
            except rospy.ServiceException as exc:
                rospy.logerr("Safety landing request failed: %s", exc)

    def _on_shutdown(self):
        if self._shutdown_started:
            return
        self._shutdown_started = True
        state, _, _ = self._snapshot()
        if state.connected and state.armed:
            try:
                self.mode_client(base_mode=0, custom_mode="AUTO.LAND")
                rospy.logwarn("Shutdown safety action: requested AUTO.LAND")
            except rospy.ServiceException:
                pass


def main():
    rospy.init_node("offboard_mission")
    mission = OffboardMission()
    try:
        mission.run()
    except (MissionError, rospy.ROSException) as exc:
        mission.abort_safely(str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
