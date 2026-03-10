# flow_captcha_service 角色配置填写指南

这份文档专门回答一个问题：

> `standalone / master / subnode` 到底应该怎么填配置，哪些字段必填，
> 哪些字段可以留空，哪些字段填错会直接导致节点不可用。

如果你只想先看结论：

- `standalone`：只填本机打码相关配置，不填集群字段
- `master`：只填主节点自身信息和调度参数，不填子节点回连字段
- `subnode`：必须填 `master_base_url / master_cluster_key / node_public_base_url / node_api_key`

---

## 1. 先理解配置从哪里来

本项目的配置来源有 3 层：

1. 代码默认值
2. `data/setting.toml`
3. 环境变量

优先级是：

```text
环境变量 > data/setting.toml > 默认值
```

常见路径：

- 模板文件：`config/setting_example.toml`
- 运行配置：`data/setting.toml`
- 自定义路径：通过 `FCS_CONFIG_FILE` 指定

---

## 2. 还有一个很容易忽略的事实

并不是所有配置都一直直接从 `setting.toml` 读取。

下面两类配置在**首次启动**时会写入数据库作为初始值：

- 管理员账号
- 浏览器相关初始配置

更具体一点：

- `admin.username`
- `admin.password`
- `captcha.browser_count`
- `captcha.browser_proxy_enabled`
- `captcha.browser_proxy_url`

这些值在数据库还没有对应记录时，会被拿来做初始化种子值。

这意味着：

- 第一次启动前写对很重要
- 如果数据库里已经有旧值，之后再改 `setting.toml`，**不会自动覆盖数据库中的已有值**
- 已经运行过的实例，更推荐从 `/admin` 后台修改管理员账号和浏览器配置

另外还有一个特殊点：

- `master` 的真正 `Cluster Key` 不是写在 `setting.toml` 里的
- 它会在主节点数据库里自动生成
- `subnode` 需要填写的是“主节点当前实际使用的 Cluster Key”

---

## 3. 角色决策先看这里

### 什么时候选 `standalone`

- 你只有一台服务
- 你想先本地跑通
- 你不需要主从调度

### 什么时候选 `master`

- 你要做集群入口
- 你希望调度多个执行节点
- 你不想让入口机自己起浏览器打码

### 什么时候选 `subnode`

- 你要让这台机器负责实际浏览器打码
- 你希望它向某个主节点注册
- 你接受它首页只显示状态页，不是用户门户

---

## 4. 先看一遍完整配置骨架

`config/setting_example.toml` 的核心结构如下：

```toml
[server]
host = "0.0.0.0"
port = 8060

[storage]
db_path = "data/captcha_service.db"

[admin]
username = "admin"
password = "admin"

[captcha]
browser_count = 1
browser_proxy_enabled = false
browser_proxy_url = ""
browser_launch_background = true
browser_score_dom_wait_seconds = 25
browser_recaptcha_settle_seconds = 3
browser_score_test_warmup_seconds = 12
browser_idle_ttl_seconds = 600
flow_timeout = 300
upsample_timeout = 300
session_ttl_seconds = 1200
node_name = "standalone-node"

[log]
level = "INFO"

[cluster]
role = "standalone"
master_base_url = ""
master_cluster_key = ""
node_public_base_url = ""
node_api_key = ""
heartbeat_interval_seconds = 15
node_weight = 100
node_max_concurrency = 0
master_node_stale_seconds = 120
master_dispatch_timeout_seconds = 45
```

你真正要改的重点，主要集中在：

- `captcha.node_name`
- `captcha.browser_count`
- `admin.username`
- `admin.password`
- `cluster.role`
- `cluster.*`

---

## 5. 公共字段怎么填

这一节是不分角色都能参考的。

### `[server]`

#### `host`

推荐值：

```toml
host = "0.0.0.0"
```

说明：

- 绝大多数 Docker 或服务器部署都建议填 `0.0.0.0`
- 如果你只允许本机访问，也可以填 `127.0.0.1`

#### `port`

推荐值：

```toml
port = 8060
```

约束：

- 允许范围是 `1 ~ 65535`

### `[storage]`

#### `db_path`

推荐值：

```toml
db_path = "data/captcha_service.db"
```

说明：

- 相对路径会相对于项目根目录解析
- 生产环境建议配合持久化挂载一起使用

### `[admin]`

#### `username`

建议：

- 不要保留默认 `admin`
- 第一次启动前就改掉更稳妥

#### `password`

建议：

- 不要保留默认 `admin`
- 首次上线前改成强密码

注意：

- 这两个字段主要用于**首次初始化数据库**
- 服务已经跑过且数据库里已有管理员账号后，再改这里不会自动覆盖旧账号

### `[captcha]`

#### `browser_count`

推荐值：

```toml
browser_count = 1
```

怎么理解：

- 这是浏览器槽位数量
- `standalone` 和 `subnode` 会真正用到
- `master` 不执行本地打码，因此不会作为本地 solve 的运行上限

建议：

- 初次部署先用 `1`
- 稳定后再逐步加到 `2`、`3`、`4`

#### `browser_proxy_enabled`

```toml
browser_proxy_enabled = false
```

如果你确实有代理池或单代理需求，再改成 `true`。

#### `browser_proxy_url`

```toml
browser_proxy_url = ""
```

说明：

- 只有在 `browser_proxy_enabled = true` 时才需要填
- 空字符串表示不使用代理

#### `browser_launch_background`

```toml
browser_launch_background = true
```

通常保持默认即可。

#### `browser_score_dom_wait_seconds`

```toml
browser_score_dom_wait_seconds = 25
```

约束：

- 允许范围是 `1.0 ~ 180.0`

#### `browser_recaptcha_settle_seconds`

```toml
browser_recaptcha_settle_seconds = 3
```

约束：

- 允许范围是 `0.0 ~ 30.0`

#### `browser_score_test_warmup_seconds`

```toml
browser_score_test_warmup_seconds = 12
```

约束：

- 允许范围是 `0.0 ~ 300.0`

#### `browser_idle_ttl_seconds`

```toml
browser_idle_ttl_seconds = 600
```

说明：

- 空闲浏览器超过这个时间会被后台回收
- 代码里会强制至少为 `60`

#### `flow_timeout`

```toml
flow_timeout = 300
```

约束：

- 允许范围是 `10 ~ 7200`

#### `upsample_timeout`

```toml
upsample_timeout = 300
```

约束：

- 允许范围是 `10 ~ 7200`

#### `session_ttl_seconds`

```toml
session_ttl_seconds = 1200
```

约束：

- 允许范围是 `120 ~ 7200`
- 代码层也会强制至少为 `120`

#### `node_name`

推荐原则：

- 每个实例都填唯一值
- 不要多台机器共用同一个名字

推荐示例：

```toml
node_name = "standalone-gz-01"
```

或者：

```toml
node_name = "master-hk-01"
```

或者：

```toml
node_name = "subnode-usw-03"
```

### `[log]`

#### `level`

推荐值：

```toml
level = "INFO"
```

可选值：

- `DEBUG`
- `INFO`
- `WARNING`
- `ERROR`
- `CRITICAL`

---

## 6. `standalone` 怎么填

### 这个角色最重要的结论

- 必须把 `cluster.role` 设成 `standalone`
- 其他集群字段都可以留空
- 重点关注浏览器槽位、管理员账号和数据库路径

### 推荐 TOML 示例

```toml
[server]
host = "0.0.0.0"
port = 8060

[storage]
db_path = "data/captcha_service.db"

[admin]
username = "admin_local"
password = "replace-with-strong-password"

[captcha]
browser_count = 2
browser_proxy_enabled = false
browser_proxy_url = ""
browser_launch_background = true
browser_score_dom_wait_seconds = 25
browser_recaptcha_settle_seconds = 3
browser_score_test_warmup_seconds = 12
browser_idle_ttl_seconds = 600
flow_timeout = 300
upsample_timeout = 300
session_ttl_seconds = 1200
node_name = "standalone-01"

[log]
level = "INFO"

[cluster]
role = "standalone"
master_base_url = ""
master_cluster_key = ""
node_public_base_url = ""
node_api_key = ""
heartbeat_interval_seconds = 15
node_weight = 100
node_max_concurrency = 0
master_node_stale_seconds = 120
master_dispatch_timeout_seconds = 45
```

### 环境变量写法示例

```env
FCS_CLUSTER_ROLE=standalone
FCS_NODE_NAME=standalone-01
FCS_ADMIN_USERNAME=admin_local
FCS_ADMIN_PASSWORD=replace-with-strong-password
FCS_BROWSER_COUNT=2
FCS_LOG_LEVEL=INFO
```

### 哪些字段在 `standalone` 下可以忽略

- `cluster.master_base_url`
- `cluster.master_cluster_key`
- `cluster.node_public_base_url`
- `cluster.node_api_key`
- `cluster.heartbeat_interval_seconds`
- `cluster.node_weight`
- `cluster.node_max_concurrency`
- `cluster.master_node_stale_seconds`
- `cluster.master_dispatch_timeout_seconds`

### 实操建议

- 第一次先从 `browser_count = 1` 起步
- 如果你没做代理，不要开启 `browser_proxy_enabled`
- `node_name` 仍然建议填唯一值，后面切集群时会更清楚

---

## 7. `master` 怎么填

### 这个角色最重要的结论

- 必须把 `cluster.role` 设成 `master`
- `master` 不执行本地打码
- 你不用给它填写“它要连接谁”的信息
- 真正要关注的是调度超时、节点离线判定和主节点自身名称

### 推荐 TOML 示例

```toml
[server]
host = "0.0.0.0"
port = 8060

[storage]
db_path = "data/master/captcha_service.db"

[admin]
username = "admin_master"
password = "replace-with-strong-password"

[captcha]
browser_count = 1
browser_proxy_enabled = false
browser_proxy_url = ""
browser_launch_background = true
browser_score_dom_wait_seconds = 25
browser_recaptcha_settle_seconds = 3
browser_score_test_warmup_seconds = 12
browser_idle_ttl_seconds = 600
flow_timeout = 300
upsample_timeout = 300
session_ttl_seconds = 1200
node_name = "master-01"

[log]
level = "INFO"

[cluster]
role = "master"
master_base_url = ""
master_cluster_key = ""
node_public_base_url = ""
node_api_key = ""
heartbeat_interval_seconds = 15
node_weight = 100
node_max_concurrency = 0
master_node_stale_seconds = 120
master_dispatch_timeout_seconds = 45
```

### 环境变量写法示例

```env
FCS_CLUSTER_ROLE=master
FCS_NODE_NAME=master-01
FCS_ADMIN_USERNAME=admin_master
FCS_ADMIN_PASSWORD=replace-with-strong-password
FCS_LOG_LEVEL=INFO
FCS_CLUSTER_MASTER_NODE_STALE_SECONDS=120
FCS_CLUSTER_MASTER_DISPATCH_TIMEOUT_SECONDS=45
```

### `master` 真正需要重点填写的字段

#### `cluster.role`

```toml
role = "master"
```

这是角色开关，必须写对。

#### `captcha.node_name`

```toml
node_name = "master-01"
```

虽然字段名字在 `captcha` 分组里，但它其实就是当前节点名称。

#### `cluster.master_node_stale_seconds`

```toml
master_node_stale_seconds = 120
```

作用：

- 超过这个时间没有心跳，主节点会把子节点判定为不可用

约束：

- 最小值 `10`

#### `cluster.master_dispatch_timeout_seconds`

```toml
master_dispatch_timeout_seconds = 45
```

作用：

- 主节点调度到子节点时，等待响应的最大时间

约束：

- 最小值 `5`

### 哪些字段在 `master` 下通常不需要填

- `cluster.master_base_url`
- `cluster.master_cluster_key`
- `cluster.node_public_base_url`
- `cluster.node_api_key`

原因：

- 这些都是 `subnode` 连接 `master` 时才需要的
- `master` 自己不需要回连别的节点

### 特别注意

#### 1. 不要把 `cluster.master_cluster_key` 当成主节点配置项去填写

主节点自己的 Cluster Key 是数据库自动生成的。

你应该做的是：

1. 启动主节点
2. 进入 `/admin`
3. 在集群配置里查看当前 Cluster Key
4. 把这个值复制给子节点的 `master_cluster_key`

#### 2. `master` 不执行本地打码

所以这些字段不会成为主节点本地 solve 的实际运行参数：

- `browser_count`
- `browser_proxy_enabled`
- `browser_proxy_url`

它们最多只会作为首次初始化种子值存在，不是主节点的主要配置重点。

---

## 8. `subnode` 怎么填

### 这个角色最重要的结论

`subnode` 是最不能随便填的角色。

下面 4 个字段缺任何一个，节点都没法正常注册：

- `cluster.master_base_url`
- `cluster.master_cluster_key`
- `cluster.node_public_base_url`
- `cluster.node_api_key`

### 推荐 TOML 示例

```toml
[server]
host = "0.0.0.0"
port = 8060

[storage]
db_path = "data/subnode/captcha_service.db"

[admin]
username = "admin_subnode"
password = "replace-with-strong-password"

[captcha]
browser_count = 2
browser_proxy_enabled = false
browser_proxy_url = ""
browser_launch_background = true
browser_score_dom_wait_seconds = 25
browser_recaptcha_settle_seconds = 3
browser_score_test_warmup_seconds = 12
browser_idle_ttl_seconds = 600
flow_timeout = 300
upsample_timeout = 300
session_ttl_seconds = 1200
node_name = "subnode-01"

[log]
level = "INFO"

[cluster]
role = "subnode"
master_base_url = "http://master.example.com:8060"
master_cluster_key = "replace-with-master-cluster-key"
node_public_base_url = "http://subnode-01.example.com:8060"
node_api_key = "replace-with-random-internal-key"
heartbeat_interval_seconds = 15
node_weight = 100
node_max_concurrency = 0
master_node_stale_seconds = 120
master_dispatch_timeout_seconds = 45
```

### 环境变量写法示例

```env
FCS_CLUSTER_ROLE=subnode
FCS_NODE_NAME=subnode-01
FCS_BROWSER_COUNT=2
FCS_CLUSTER_MASTER_BASE_URL=http://master.example.com:8060
FCS_CLUSTER_MASTER_CLUSTER_KEY=replace-with-master-cluster-key
FCS_CLUSTER_NODE_PUBLIC_BASE_URL=http://subnode-01.example.com:8060
FCS_CLUSTER_NODE_API_KEY=replace-with-random-internal-key
FCS_CLUSTER_HEARTBEAT_INTERVAL_SECONDS=15
FCS_CLUSTER_NODE_WEIGHT=100
FCS_CLUSTER_NODE_MAX_CONCURRENCY=0
```

### 4 个必填字段逐个解释

#### `cluster.master_base_url`

```toml
master_base_url = "http://master.example.com:8060"
```

它表示：

- 当前子节点要去连接哪个主节点

你应该填：

- 子节点机器能访问到的主节点地址

你不应该填：

- 一个只有浏览器能访问、但服务端访问不到的地址

#### `cluster.master_cluster_key`

```toml
master_cluster_key = "replace-with-master-cluster-key"
```

它表示：

- 当前子节点连接主节点时使用的集群密钥

这个值的来源：

1. 先启动主节点
2. 去主节点 `/admin`
3. 查看当前 Cluster Key
4. 原样复制到子节点这里

#### `cluster.node_public_base_url`

```toml
node_public_base_url = "http://subnode-01.example.com:8060"
```

这是最容易填错的字段。

它表示：

- 主节点回调当前子节点时，要访问哪个地址

你应该填：

- **主节点能访问到的当前子节点地址**

你绝对不要填：

- `http://127.0.0.1:8060`
- `http://localhost:8060`
- `http://0.0.0.0:8060`

原因：

- 这些地址对当前机器自己来说可能能用
- 但对主节点来说通常不可达
- 代码里也会直接把这些值判定为无效

#### `cluster.node_api_key`

```toml
node_api_key = "replace-with-random-internal-key"
```

它表示：

- 主节点调用当前子节点内部接口时使用的认证 Key

建议：

- 使用一个随机长字符串
- 不要和门户 API Key、管理员密码共用

### `subnode` 里其他常用字段怎么填

#### `cluster.heartbeat_interval_seconds`

```toml
heartbeat_interval_seconds = 15
```

作用：

- 子节点多久向主节点发送一次心跳

约束：

- 最小值 `5`

建议：

- 一般保持 `15`

#### `cluster.node_weight`

```toml
node_weight = 100
```

作用：

- 主节点调度时的权重参考值

约束：

- 最小值 `1`

建议：

- 默认先用 `100`
- 性能更强的节点可以给更高权重

#### `cluster.node_max_concurrency`

```toml
node_max_concurrency = 0
```

这是另一个最容易误解的字段。

它表示：

- 子节点上报给主节点的调度并发上限

实际运行里采用：

```text
effective_capacity = min(browser_count, node_max_concurrency)
```

但要注意：

- 在 `setting.toml` 或环境变量里，写 `0` 的含义是
  “自动跟随 `browser_count`”
- 代码会把 `0`、空值、非法值都回退成 `browser_count`

所以：

- `browser_count = 4`
- `node_max_concurrency = 0`

最终等价于：

```text
effective_capacity = 4
```

如果你写成：

- `browser_count = 4`
- `node_max_concurrency = 2`

最终就是：

```text
effective_capacity = 2
```

适合用在：

- 机器虽然能开 4 个浏览器槽位
- 但你只想让主节点最多同时派 2 个 solve

补充说明：

- 如果你是直接改 `setting.toml` 或环境变量，写 `0` 没问题
- 如果你是从当前管理后台页面修改，页面保存逻辑只接受正整数
- 也就是说，想用“自动跟随 `browser_count`”时，优先通过文件或环境变量配置

### `subnode` 常见填写场景

#### 场景 A：官方 stack，同一 Docker 网络

可直接参考：

```toml
master_base_url = "http://flow-captcha-master:8060"
node_public_base_url = "http://flow-captcha-subnode:8060"
```

#### 场景 B：同一台机器，不同端口

假设：

- master 对外端口是 `8060`
- subnode 对外端口是 `8061`

可以写：

```toml
master_base_url = "http://host.docker.internal:8060"
node_public_base_url = "http://host.docker.internal:8061"
```

前提：

- 你的运行环境支持 `host.docker.internal`
- 比如 Docker Desktop

#### 场景 C：跨机器部署

例如：

- master 在 `10.0.0.10:8060`
- subnode 在 `10.0.0.11:8060`

可以写：

```toml
master_base_url = "http://10.0.0.10:8060"
node_public_base_url = "http://10.0.0.11:8060"
```

如果用了域名，也可以写域名。

---

## 9. 哪些字段在不同角色下会被忽略

### `standalone`

通常可忽略：

- `cluster.master_base_url`
- `cluster.master_cluster_key`
- `cluster.node_public_base_url`
- `cluster.node_api_key`
- 所有调度相关字段

### `master`

通常可忽略：

- `cluster.master_base_url`
- `cluster.master_cluster_key`
- `cluster.node_public_base_url`
- `cluster.node_api_key`

同时要注意：

- `master` 不会执行本地 solve
- 浏览器相关字段不是它的主要运行配置

### `subnode`

不能忽略：

- `cluster.master_base_url`
- `cluster.master_cluster_key`
- `cluster.node_public_base_url`
- `cluster.node_api_key`

其中最容易填错的是：

- `cluster.node_public_base_url`

---

## 10. 环境变量和 TOML 怎么选

### 推荐原则

#### 本地开发

推荐：

- 基础配置写进 `data/setting.toml`
- 少量覆盖项用环境变量

#### Docker / 容器编排

推荐：

- 路径、角色、地址、密钥用环境变量
- 其他默认值保留在模板或镜像默认配置里

### 对照关系

常见映射如下：

- `cluster.role` -> `FCS_CLUSTER_ROLE`
- `captcha.node_name` -> `FCS_NODE_NAME`
- `cluster.master_base_url` -> `FCS_CLUSTER_MASTER_BASE_URL`
- `cluster.master_cluster_key` -> `FCS_CLUSTER_MASTER_CLUSTER_KEY`
- `cluster.node_public_base_url` -> `FCS_CLUSTER_NODE_PUBLIC_BASE_URL`
- `cluster.node_api_key` -> `FCS_CLUSTER_NODE_API_KEY`
- `cluster.heartbeat_interval_seconds` -> `FCS_CLUSTER_HEARTBEAT_INTERVAL_SECONDS`
- `cluster.node_weight` -> `FCS_CLUSTER_NODE_WEIGHT`
- `cluster.node_max_concurrency` -> `FCS_CLUSTER_NODE_MAX_CONCURRENCY`
- `cluster.master_node_stale_seconds` -> `FCS_CLUSTER_MASTER_NODE_STALE_SECONDS`
- `cluster.master_dispatch_timeout_seconds` -> `FCS_CLUSTER_MASTER_DISPATCH_TIMEOUT_SECONDS`

---

## 11. 推荐检查清单

### `standalone` 启动前检查

- `cluster.role = "standalone"` 是否正确
- `browser_count` 是否符合机器承载能力
- 管理员账号密码是否已经改掉默认值
- `db_path` 是否位于持久化目录

### `master` 启动前检查

- `cluster.role = "master"` 是否正确
- `node_name` 是否唯一
- `master_node_stale_seconds` 是否合理
- `master_dispatch_timeout_seconds` 是否合理
- 数据目录是否和 `subnode` 隔离

### `subnode` 启动前检查

- `cluster.role = "subnode"` 是否正确
- `master_base_url` 是否真能连到主节点
- `master_cluster_key` 是否与主节点当前值完全一致
- `node_public_base_url` 是否是主节点可访问地址
- `node_public_base_url` 是否误填成 `127.0.0.1/localhost/0.0.0.0`
- `node_api_key` 是否已经填写
- `browser_count` 和 `node_max_concurrency` 是否符合你的并发预期

---

## 12. 最后给一个最实用的建议

如果你不确定怎么填，按下面顺序最稳：

1. 先用 `standalone` 跑通
2. 再启一个 `master`
3. 最后再加 `subnode`
4. `subnode` 只重点检查 4 个必填字段

如果子节点一直注册不上，先查下面 3 项：

1. `master_base_url` 是否可达
2. `master_cluster_key` 是否和主节点一致
3. `node_public_base_url` 是否被填成了本机回环地址

这 3 个问题，基本覆盖了大多数 `subnode` 启动失败场景。
