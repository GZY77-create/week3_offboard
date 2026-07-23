# week3_offboard

基于 ROS Noetic、PX4 v1.14、Gazebo Classic 11 和 MAVROS 的 Offboard 正方形航线实操。

![任务一验收总览：Gazebo、任务完成状态与 rqt_graph](images/task1_acceptance_overview.png)

## 实操结果

任务航线：起飞到 2 m、悬停 5 s、飞行边长 5 m 的正方形、回到起点、自动降落，确认接地后解除武装。航点容差为 0.25 m。

## 环境

- Ubuntu 20.04
- ROS Noetic
- PX4-Autopilot v1.14.x，默认路径 `/root/PX4-Autopilot`
- Gazebo Classic 11
- MAVROS 1.20.x

首次安装 MAVROS 后需要安装 GeographicLib 数据：

```bash
sudo /opt/ros/noetic/lib/mavros/install_geographiclib_datasets.sh
```

## 编译

```bash
cd /root/catkin_ws
source /opt/ros/noetic/setup.bash
catkin_make
source devel/setup.bash
```

## 一键运行

脚本必须在宿主机终端执行，不要进入容器后运行：

```bash
cd /path/to/catkin_ws/src/week3_offboard
xhost +si:localuser:root
./scripts/start_week3_host.sh
```

将 `/path/to/catkin_ws` 替换为宿主机工作空间的实际路径。容器未运行时脚本会
自动启动它；容器名不是 `ros-noetic` 时使用：

```bash
ROS_CONTAINER=实际容器名 ./scripts/start_week3_host.sh
```

脚本会自动启动 ROS Master、PX4 SITL + Gazebo、MAVROS、Offboard 任务、
`rqt_graph` 和 `/mavros/state` 状态监视，并根据当前屏幕分辨率排列 5 个终端。

## 手动运行

终端 1 启动 PX4 SITL、Iris 和 Gazebo：

```bash
cd /root/PX4-Autopilot
make px4_sitl gazebo-classic_iris
```

终端 2 一次启动 MAVROS 和 Offboard 节点：

```bash
source /opt/ros/noetic/setup.bash
source /root/catkin_ws/devel/setup.bash
roslaunch week3_offboard offboard_mission.launch
```

如果 MAVROS 已经运行，避免重复占用 UDP 端口：

```bash
roslaunch week3_offboard offboard_mission.launch start_mavros:=false
```

可调整航线：

```bash
roslaunch week3_offboard offboard_mission.launch \
  altitude:=2.0 side_length:=2.0 hover_seconds:=5.0
```

## 飞行流程

1. 等待 `/mavros/state` 的 `connected=True` 和有效本地位置。
2. 以 20 Hz 预发送起飞目标点 5 秒。
3. 请求 `OFFBOARD`，确认成功后才请求解锁。
4. 起飞到 2 m，悬停 5 秒。
5. 依次飞过四个正方形航点并返回起点。
6. 请求 `AUTO.LAND`，通过 `/mavros/extended_state` 确认接地后再解除武装。

## 节点通信关系

一键脚本会将 `rqt_graph` 设为简洁的 **Nodes only** 视图，便于录屏展示控制节点与 MAVROS 的关系。核心链路为：

```text
PX4 SITL <--UDP/MAVLink--> /mavros <--ROS topics/services--> /offboard_mission

/mavros/state ------------------------------> /offboard_mission
/mavros/local_position/pose ----------------> /offboard_mission
/mavros/extended_state ---------------------> /offboard_mission
/offboard_mission -- setpoint_position/local --> /mavros --> PX4
/offboard_mission -- cmd/arming service ------> /mavros --> PX4
/offboard_mission -- set_mode service --------> /mavros --> PX4
```

| 名称 | 类型 | 方向 | 用途 |
|---|---|---|---|
| `/mavros/state` | `mavros_msgs/State` | MAVROS → 控制节点 | 获取连接、解锁和模式状态；未连接时禁止解锁。 |
| `/mavros/local_position/pose` | `geometry_msgs/PoseStamped` | MAVROS → 控制节点 | 获取 ENU 本地位置，用于航点到达判定和误差保护。 |
| `/mavros/extended_state` | `mavros_msgs/ExtendedState` | MAVROS → 控制节点 | 读取 `landed_state`，确认接地后才允许解除武装。 |
| `/mavros/setpoint_position/local` | `geometry_msgs/PoseStamped` | 控制节点 → MAVROS | 以 20 Hz 发送本地位置目标。 |
| `/mavros/cmd/arming` | `mavros_msgs/CommandBool` 服务 | 控制节点 → MAVROS | 请求解锁或上锁。 |
| `/mavros/set_mode` | `mavros_msgs/SetMode` 服务 | 控制节点 → MAVROS | 请求 `OFFBOARD` 和 `AUTO.LAND`。 |

MAVROS 使用 ENU 坐标系（x 向东、y 向北、z 向上），并负责与 PX4 的 NED 坐标系转换。

## 异常处理

节点实现了四项保护：

1. **未连接不解锁**：等待 FCU 和本地位置；解锁前再次检查 `state.connected`，断开时抛出安全异常。
2. **模式切换失败重试**：`OFFBOARD`/`AUTO.LAND` 每 2 秒请求一次，持续确认反馈，30 秒超时后安全中止。
3. **位置误差过大悬停**：误差超过 8 m 时，将当前位置设为目标并连续发布 3 秒，随后进入 `AUTO.LAND`。该阈值高于正常的 5 m 单段航线，不会将第一个角点误判为异常。
4. **Ctrl+C 安全退出**：ROS shutdown 回调检查是否仍解锁；若在空中则请求 `AUTO.LAND`。

任何飞行阶段检测到 FCU 断开、意外退出 `OFFBOARD` 或航点超时，也会中止任务并请求自动降落。

## 任务一实际报错与解决过程

### 1. ROS Master 无法连接

**报错：**

```text
ERROR: Unable to communicate with master!
XmlRpcClient::writeRequest: write error (Connection refused)
```

**原因：** 原脚本依赖 `roslaunch` 自动创建 ROS Master，并用固定 `sleep` 安排启动顺序。不同进程的实际启动时间不固定，监视节点可能在 Master 尚未就绪时运行。

**解决：** `start_week3_host.sh` 新增独立的 `0-ROS Master` 终端运行 `roscore`。MAVROS 循环检查 Master，监视和任务节点则等待 `/mavros/state` 出现，不再只依赖固定延时。

**验证：** `rosnode list` 能稳定显示 `/mavros`、`/offboard_mission` 和 `/rosout`，`/mavros/state` 中显示 `connected: True`。

### 2. 起飞后未飞正方形就中止

**报错：**

```text
Mission aborted: Position error 5.02 m exceeded limit; holding
```

**原因：** 正方形边长和 `max_position_error` 都是 5 m。从起点切换到 `corner_1` 时，正常初始距离已经约为 5 m，叠加少量位置波动后成为 5.02 m，因此被误判为飞行异常。

**解决：** launch 中将 `max_position_error` 改为 8 m，高于正常的 5 m 航段。Python 节点还会检查该阈值必须大于 `side_length`。超过阈值时仍保留原地悬停 3 秒和 `AUTO.LAND` 保护。

**验证：** 任务日志应依次出现：

```text
Reached takeoff
Reached corner_1
Reached corner_2
Reached corner_3
Reached corner_4
Square complete
```

### 3. PX4 重复启动

**报错：**

```text
ERROR: PX4 server already running for instance 0
```

**原因：** 上一轮仿真结束时，PX4、Gazebo 或 MAVROS 子进程没有全部退出，再次启动后与旧实例冲突。

**解决：** 先确认飞行器已接地且 `armed: False`，再清理残留任务：

```bash
docker restart ros-noetic
```

之后只在宿主机运行一次 `./scripts/start_week3_host.sh`。

### 4. Gazebo 只有服务、没有画面

**现象：** 容器内有 `gzserver`，但没有 `gzclient`，宿主机不显示 Gazebo GUI。

**原因：** 容器内的 root 用户未获得当前宿主机 X11 会话的图形窗口权限。

**解决：** 在宿主机执行：

```bash
xhost +si:localuser:root
./scripts/start_week3_host.sh
```

容器需要挂载 `/tmp/.X11-unix` 并设置 `DISPLAY=:0`。一键脚本必须在宿主机终端运行，不应在容器内运行。

**验证：** 容器内同时存在 `gzserver`、`gzclient` 和 `px4`，宿主机能看到 Iris 与 Gazebo 场景。

### 5. 降落时重复拒绝解除武装

**报错：**

```text
WARN [commander] Disarming denied! Not landed
```

**原因：** 旧逻辑切换到 `AUTO.LAND` 后立即循环请求解除武装。PX4 检测到飞行器仍在空中，因此出于安全考虑拒绝请求。

**解决：** 节点新增订阅 `/mavros/extended_state`。请求 `AUTO.LAND` 后先等待 `landed_state == LANDED_STATE_ON_GROUND`，确认接地后才解除武装；如果 PX4 已自动解除武装，直接判定任务完成。

**验证：** 降落时应看到 `AUTO.LAND: waiting for touchdown`，最终看到 `Mission complete: landed and disarmed`。地面状态为 `landed_state: 1` 和 `armed: False`。

## 检查与排错

```bash
rostopic echo /mavros/state
rostopic hz /mavros/setpoint_position/local
rostopic echo /mavros/local_position/pose
rostopic echo /mavros/extended_state
rqt_graph
```

## 验收录屏

- 文件：[`videos/week3_task1_demo.mp4`](videos/week3_task1_demo.mp4)
- 时长：2 分 03 秒
- 分辨率：2560 × 1600，30 FPS

[▶ 查看或下载完整验收录屏](videos/week3_task1_demo.mp4)

录屏包含 Gazebo 中的完整起飞、悬停、正方形航线、返航和降落过程；终端最终
显示 `Mission complete: landed and disarmed`，`/mavros/state` 显示
`connected: True`、`armed: False`，并展示 `rqt_graph` 中
`/offboard_mission` 与 `/mavros` 的通信关系。
