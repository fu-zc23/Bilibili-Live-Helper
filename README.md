# Bilibili Live Helper

一个简洁的 B 站直播亲密度任务脚本，支持：

- 扫码登录并保存 Cookie
- 自动获取当前账号的所有粉丝牌直播间
- 对开播房间点赞
- 向所有房间发送弹幕
- 对开播房间挂机观看
- 记录每个房间当天的点赞 / 弹幕完成状态，以及观看累计分钟数

## 安装依赖

```bash
pip install -r requirements.txt
```

## 准备配置

复制示例配置：

```bash
cp live_helper_config.example.json live_helper_config.json
```

然后修改 `live_helper_config.json`：

- `cookie`：可留空，首次运行时会自动扫码登录并写回配置文件
- `danmaku_messages`：弹幕内容列表
- `danmaku_count`：每个房间发送的弹幕条数
- `danmaku_interval_min` / `danmaku_interval_max`：两条弹幕之间的随机等待区间，单位秒
- `like_count`：每个开播房间的总点赞数，未开播房间会自动跳过点赞
- `like_batch_size`：每次请求点赞数，范围 `1-10`
- `like_interval_min` / `like_interval_max`：两次点赞请求之间的随机等待区间，单位秒
- `watch_minutes`：每个房间当天目标观看分钟数，`0` 表示关闭
- `watch_session_minutes`：单次运行最多连续观看同一房间多少分钟

## 运行方式

```bash
python main.py
```

只扫码登录并保存 Cookie：

```bash
python main.py --login
```

扫码后继续执行任务：

```bash
python main.py --login --run-after-login
```

也可以通过命令行临时覆盖部分配置，例如：

```bash
python main.py --danmaku-count 1 --like-count 20 --like-batch-size 10 --watch-minutes 150 --watch-session-minutes 30
```

## 说明

- 如果配置里没有本地 Cookie，脚本会自动进入扫码登录流程，并在终端直接输出二维码。
- 点赞和观看任务仅在目标直播间处于开播状态时执行，发弹幕会对所有解析到的目标房间执行。
- 脚本会在配置文件同目录生成 `task_state.json`，按天记录每个房间是否已完成点赞 / 弹幕任务，以及当天已观看分钟数；跨天会自动重新开始。
- 观看任务会按“当天已观看分钟数从少到多”选择开播房间，单房间连续观看，避免每次都只看排在前面的直播间。
- `watch_minutes` 控制每个房间当天的目标观看时长，`watch_session_minutes` 控制单次运行最多连续观看同一房间多久。
- 推荐用 `cron` 每隔几分钟运行一次，配合 `task_state.json` 去重。

例如每 `30` 分钟跑一次：

```bash
(crontab -l 2>/dev/null | grep -v 'bilibili-live-helper/main.py'; \
echo 'CRON_TZ=Asia/Shanghai'; \
echo '*/30 * * * * cd /home/ubuntu/bilibili-live-helper && /usr/bin/python3 main.py >> /home/ubuntu/bilibili-live-helper/cron.log 2>&1') | crontab -
```
