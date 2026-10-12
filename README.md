# Bilibili Live Helper

一个简洁的 B 站直播亲密度任务脚本，支持：

- 扫码登录并保存 Cookie
- 自动获取当前账号的所有粉丝牌直播间
- 自动获取每个主播的亲密度任务完成状态
- 对开播房间点赞
- 向所有房间发送弹幕
- 对开播房间挂机观看
- 粉丝牌熄灭后自动点亮
- 天选时刻自动参与（独立 lottery 包，见下文）

不再需要手动配置任务次数，脚本自动获取每日任务进度，计算还需多少操作。

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
- `danmaku_interval_min` / `danmaku_interval_max`：两条弹幕之间的随机等待区间，单位秒
- `like_batch_size`：每次请求点赞数，范围 `1-10`
- `like_interval_min` / `like_interval_max`：两次点赞请求之间的随机等待区间，单位秒
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
python main.py --like-batch-size 10 --watch-session-minutes 30
```

## 说明

- 如果配置里没有本地 Cookie，脚本会自动进入扫码登录流程，并在终端直接输出二维码。
- 点赞和观看任务仅在目标直播间处于开播状态时执行，发弹幕对所有解析到的目标房间都执行。
- 观看任务会按"当天已完成分钟数从少到多"选择开播房间，单房间连续观看。
- 粉丝牌熄灭时，主脚本会先执行点亮任务（按接口给出的次数发弹幕，未开播房间也可发；
  弹幕未能点亮且房间开播时用点赞兜底），点亮后自动恢复每日任务流程。
- 推荐用 `cron` 定时运行。

例如每 `30` 分钟跑一次：

```bash
(crontab -l 2>/dev/null | grep -v 'bilibili-live-helper/main.py'; \
echo 'CRON_TZ=Asia/Shanghai'; \
echo '*/30 * * * * cd /home/ubuntu/bilibili-live-helper && /usr/bin/python3 main.py >> /home/ubuntu/bilibili-live-helper/cron.log 2>&1') | crontab -
```

## 天选时刻自动参与

`lottery/` 包是独立的天选时刻功能（复用主脚本的配置和 Cookie），包含两个部分：

- **扫描入口**：`python3 -m lottery`，每轮扫描关注列表中所有主播，
  发现进行中的天选后自动参与。建议用 cron 短轮询运行（天选一般只持续 5-15 分钟，
  间隔 3 分钟左右即可）。
- **驻留子进程**：`lottery/stay.py`，参与成功后自动派生。天选要求「开奖时刻需在
  直播间内」，在线判定基于弹幕 WebSocket 连接，故该进程会维持 WS 连接 + 心跳，
  挂到开奖后自动退出，无需管理。

### 参与规则

- 扫描范围：关注列表里的全部直播间，房间号缓存在 `lottery_state.json`，
  新关注的用户自动增量解析。
- 跳过需要消费的送礼类天选，只参与弹幕口令类。
- 参与门槛不做本地判断，由 B 站服务器在参与时裁决，不满足会记录 WARN 日志。
- state 中的参与记录以天选 id 去重，7 天自动过期清理。

### 运行方式

```bash
python3 -m lottery
```

例如每 `3` 分钟跑一次：

```bash
(crontab -l 2>/dev/null | grep -v 'python3 -m lottery'; \
echo '*/3 * * * * cd /home/ubuntu/bilibili-live-helper && /usr/bin/python3 -m lottery >> /home/ubuntu/bilibili-live-helper/lottery.log 2>&1') | crontab -
```

日志写入 `lottery.log`，运行状态记录在 `lottery_state.json`。