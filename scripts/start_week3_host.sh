#!/usr/bin/env bash
set -euo pipefail

# This script must run on the host. The terminals use the host user's default
# GNOME Terminal profile; only the PX4/ROS commands run inside the container.
CONTAINER="${ROS_CONTAINER:-ros-noetic}"

if ! command -v docker >/dev/null 2>&1; then
  echo "宿主机未安装 Docker。" >&2
  exit 1
fi

if ! command -v gnome-terminal >/dev/null 2>&1; then
  echo "宿主机未安装 gnome-terminal。" >&2
  exit 1
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Docker 容器 '$CONTAINER' 不存在或当前用户无权访问 Docker。" >&2
  echo "如果容器名不同，请用: ROS_CONTAINER=实际名称 $0" >&2
  exit 1
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != true ]; then
  docker start "$CONTAINER" >/dev/null
fi

if docker exec "$CONTAINER" bash -lc \
  'pgrep -x px4 >/dev/null || pgrep -x gzserver >/dev/null || pgrep -x mavros_node >/dev/null'; then
  echo "容器中已有 PX4、Gazebo 或 MAVROS 进程，请先正常结束上一轮任务。" >&2
  exit 1
fi

SCREEN_SIZE="$(
  xrandr --current 2>/dev/null |
    awk '/\*/ {print $1; exit}' || true
)"
SCREEN_SIZE="${TASK1_SCREEN_SIZE:-${SCREEN_SIZE:-1920x1080}}"
SCREEN_WIDTH="${SCREEN_SIZE%x*}"
SCREEN_HEIGHT="${SCREEN_SIZE#*x}"

TERM_COLS=$(((SCREEN_WIDTH / 3 - 40) / 9))
TERM_ROWS=$(((SCREEN_HEIGHT / 2 - 100) / 18))
((TERM_COLS < 42)) && TERM_COLS=42
((TERM_COLS > 66)) && TERM_COLS=66
((TERM_ROWS < 12)) && TERM_ROWS=12
((TERM_ROWS > 20)) && TERM_ROWS=20

X_LEFT=10
X_CENTER=$((SCREEN_WIDTH / 3 + 10))
X_RIGHT=$((SCREEN_WIDTH * 2 / 3 + 10))
Y_TOP=40
Y_BOTTOM=$((SCREEN_HEIGHT / 2 + 10))

open_terminal() {
  local title="$1"
  local x="$2"
  local y="$3"
  local command="$4"

  gnome-terminal --window --title="$title" \
    --geometry="${TERM_COLS}x${TERM_ROWS}+${x}+${y}" -- bash -lc \
    'docker exec -it "$1" bash -lc "$2"; exec bash' \
    bash "$CONTAINER" "$command"
}

open_terminal "0-ROS Master" "$X_LEFT" "$Y_TOP" \
  'source /opt/ros/noetic/setup.bash; roscore'

open_terminal "1-PX4+Gazebo" "$X_CENTER" "$Y_TOP" \
  'cd /root/PX4-Autopilot; make px4_sitl gazebo-classic_iris'

open_terminal "2-MAVROS" "$X_RIGHT" "$Y_TOP" \
  'source /opt/ros/noetic/setup.bash; until rostopic list >/dev/null 2>&1; do sleep 1; done; roslaunch mavros px4.launch fcu_url:=udp://:14540@127.0.0.1:14580'

open_terminal "3-监视+验收证据" "$X_LEFT" "$Y_BOTTOM" \
  'source /opt/ros/noetic/setup.bash; \
   until rostopic list 2>/dev/null | grep -qx /mavros/state; do sleep 1; done; \
   cfg=/root/.config/ros.org/rqt_gui.ini; \
   if [ -f "$cfg" ]; then \
     sed -i -E \
       -e "/graph_type_combo_box_index=/s/=.*/=0/" \
       -e "/dead_sinks_check_box_state=/s/=.*/=true/" \
       -e "/leaf_topics_check_box_state=/s/=.*/=true/" \
       -e "/quiet_check_box_state=/s/=.*/=true/" \
       -e "/unreachable_check_box_state=/s/=.*/=true/" \
       "$cfg"; \
   fi; \
   echo "===== MAVROS 关键话题 ====="; \
   rostopic list | grep -E "^/mavros/(state|local_position/pose|setpoint_position/local)$"; \
   echo "===== MAVROS 关键服务 ====="; \
   rosservice list | grep -E "^/mavros/(cmd/arming|set_mode)$"; \
   echo "===== /mavros/state 实时状态 ====="; \
   rqt_graph & rostopic echo /mavros/state'

open_terminal "4-飞行任务" "$X_CENTER" "$Y_BOTTOM" \
  'source /opt/ros/noetic/setup.bash; until rostopic list 2>/dev/null | grep -qx /mavros/state; do sleep 1; done; source /root/catkin_ws/devel/setup.bash; roslaunch week3_offboard offboard_mission.launch start_mavros:=false'
