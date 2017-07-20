#!/usr/bin/env python

import actionlib
from actionlib_msgs.msg import GoalStatus
from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
from geometry_msgs.msg import Pose
from interactive_markers.interactive_marker_server import InteractiveMarkerServer
from map_annotator.msg import PoseNames, UserAction
from map_annotator.msg import GoToLocationAction, GoToLocationResult
from visualization_msgs.msg import InteractiveMarker, InteractiveMarkerControl, InteractiveMarkerFeedback
from visualization_msgs.msg import Marker
import pickle
import rospy


def wait_for_time():
    """Wait for simulated time to begin.
    """
    while rospy.Time().now().to_sec() == 0:
        pass


class PoseMarkers(object):
    def __init__(self, database, im_server):
        self._database = database
        self._im_server = im_server

    def start(self):
        for name in self._database.list():
            pose = self._database.get(name)
            self.add(name, pose)
        self._im_server.applyChanges()

    def create(self, name):
        """Bring up a new marker.
        """
        pose = Pose()
        pose.orientation.w = 1
        pose.position.z = 0.05
        self.add(name, pose)
        self._im_server.applyChanges()

    def add(self, name, pose):
        """Bring up a new marker.
        """
        int_marker = self._make_marker(name, pose)
        self._im_server.insert(int_marker)
        self._im_server.setCallback(name, self._update_pose)

    def delete(self, name):
        self._im_server.erase(name)
        self._im_server.applyChanges()

    def get(self, name):
        return self._im_server.get(name)

    def _make_marker(self, name, pose):
        """Creates a new interactive marker.

        Args:
            name: The name of the marker.
            pose: The geometry_msgs/Pose of the marker.
        """
        int_marker = InteractiveMarker()
        int_marker.header.frame_id = 'map'
        int_marker.name = name
        int_marker.description = name
        int_marker.pose = pose

        arrow_marker = Marker()
        arrow_marker.type = Marker.ARROW
        arrow_marker.pose.orientation.w = 1
        arrow_marker.scale.x = 0.45
        arrow_marker.scale.y = 0.05
        arrow_marker.scale.z = 0.05
        arrow_marker.color.r = 0.0
        arrow_marker.color.g = 0.5
        arrow_marker.color.b = 0.5
        arrow_marker.color.a = 1.0

        arrow_control = InteractiveMarkerControl()
        arrow_control.orientation.w = 1
        arrow_control.orientation.y = 1
        arrow_control.interaction_mode = InteractiveMarkerControl.MOVE_PLANE
        arrow_control.markers.append(arrow_marker)
        arrow_control.always_visible = True
        int_marker.controls.append(arrow_control)

        control = InteractiveMarkerControl()
        control.orientation.w = 1
        control.orientation.y = 1
        control.interaction_mode = InteractiveMarkerControl.ROTATE_AXIS
        int_marker.controls.append(control)
        return int_marker

    def _update_pose(self, feedback):
        if feedback.event_type == InteractiveMarkerFeedback.POSE_UPDATE:
            self._database.add(feedback.marker_name, feedback.pose)


class PoseDatabase(object):
    def __init__(self, db_path):
        self._db_path = db_path
        self._poses = {}  # Maps names (strings) to geometry_msgs/Pose

    def add(self, name, pose):
        self._poses[name] = pose

    def get(self, name):
        if name in self._poses:
            return self._poses[name]
        else:
            return None

    def list(self):
        return self._poses.keys()

    def delete(self, name):
        if name in self._poses:
            del self._poses[name]

    def load(self):
        try:
            with open(self._db_path, 'r') as f:
                self._poses = pickle.load(f)
        except IOError as e:
            rospy.logwarn('No database: {}'.format(e))

    def save(self):
        try:
            with open(self._db_path, 'w') as f:
                pickle.dump(self._poses, f)
        except IOError as e:
            rospy.logerr('Error saving database: {}'.format(e))


def is_done_state(state):
    """Returns True if the actionlib state is a "done" state, False otherwise.

    Args:
        state: int, an actionlib_msgs/GoalStatus value.
    """
    return (state == GoalStatus.RECALLED or state == GoalStatus.REJECTED
            or state == GoalStatus.PREEMPTED or state == GoalStatus.ABORTED
            or state == GoalStatus.SUCCEEDED or state == GoalStatus.LOST)


class Server(object):
    def __init__(self, db, list_pub, marker_server, move_base_client):
        self._db = db
        self._list_pub = list_pub
        self._marker_server = marker_server
        self._move_base_client = move_base_client
        self._as = actionlib.SimpleActionServer(
            '/map_annotator/go_to_location',
            GoToLocationAction,
            execute_cb=self.execute_goto,
            auto_start=False)

    def start(self):
        self._db.load()
        self._as.start()
        self._marker_server.start()
        self._publish_poses()

    def handle_user_action(self, action):
        if action.command == UserAction.CREATE:
            self._marker_server.create(action.name)
            pose = self._marker_server.get(action.name).pose
            self._db.add(action.name, pose)
            self._publish_poses()
        elif action.command == UserAction.DELETE:
            self._marker_server.delete(action.name)
            self._db.delete(action.name)
            self._publish_poses()
        elif action.command == UserAction.GOTO:
            pose = self._db.get(action.name)
            if pose is None:
                rospy.logerr('No pose named {}'.format(action.name))
            else:
                goal = MoveBaseGoal()
                goal.target_pose.header.frame_id = 'map'
                goal.target_pose.header.stamp = rospy.Time().now()
                goal.target_pose.pose = pose
                self._move_base_client.send_goal(goal)
        else:
            rospy.logwarn('Unknown command: {}'.format(action.command))

    def execute_goto(self, goal):
        result = GoToLocationResult()
        pose = self._db.get(goal.name)
        if pose is None:
            result.error = 'No pose named {}'.format(goal.name)
            self._as.set_aborted(result, result.error)
            rospy.logerr(result.error)
            return
        else:
            goal = MoveBaseGoal()
            goal.target_pose.header.frame_id = 'map'
            goal.target_pose.header.stamp = rospy.Time().now()
            goal.target_pose.pose = pose
            self._move_base_client.send_goal(goal)
            state = self._move_base_client.get_state()
            while not rospy.is_shutdown():
                if self._as.is_preempt_requested():
                    result.error = 'GoToLocationAction was preempted'
                    self._as.set_preempted(result, result.error)
                    rospy.logerr(result.error)
                    return
                if is_done_state(self._move_base_client.get_state()):
                    if self._move_base_client.get_state(
                    ) == GoalStatus.SUCCEEDED:
                        self._as.set_succeeded(result)
                        return
                    else:
                        result.error = 'The robot was unable to navigate to {}'.format(
                            goal.name)
                        self._as.set_aborted(result, result.error)
                        rospy.logerr(result.error)
                        return
                rospy.sleep(0.1)

    def _publish_poses(self):
        pn = PoseNames()
        pn.names.extend(self._db.list())
        self._list_pub.publish(pn)


def main():
    rospy.init_node('map_annotator_server')
    myargv = rospy.myargv()
    if len(myargv) < 2:
        print 'Usage: rosrun map_annotator server.py path/to/db'
        return
    db_path = myargv[1]
    wait_for_time()

    database = PoseDatabase(db_path)
    list_pub = rospy.Publisher(
        '/map_annotator/pose_names', PoseNames, latch=True, queue_size=1)
    im_server = InteractiveMarkerServer('/map_annotator/map_poses')
    marker_server = PoseMarkers(database, im_server)
    move_base = actionlib.SimpleActionClient('/move_base', MoveBaseAction)
    server = Server(database, list_pub, marker_server, move_base)
    user_action_sub = rospy.Subscriber('/map_annotator/user_actions',
                                       UserAction, server.handle_user_action)

    rospy.sleep(0.5)
    marker_server.start()
    server.start()

    def handle_shutdown():
        pn = PoseNames()
        list_pub.publish(pn)
        database.save()

    rospy.on_shutdown(handle_shutdown)

    rospy.spin()


if __name__ == '__main__':
    main()
