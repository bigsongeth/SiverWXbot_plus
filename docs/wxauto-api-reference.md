# wxauto / wxautox4 API 参考（本项目专用）

> **这份文件是干什么的**：以后改 `wxbot_core.py` 或插件里碰微信 UI 的底层代码时，先来这里查方法签名、返回值和坑，
> 不用再去翻文档站或者 SSH 到生产机 inspect。文档没写的参数这里不补；本机 inspect 与文档不一致的地方用 ⚠️ 单独标出。

- **整理日期**：2026-09-08
- **文档站**：https://docs.wxauto.org/ （站点本身不标版本号；正文里出现的版本说明只有「v40.1.8 及以上支持 `CALLBACK_STOP_SIGN`」「v40.1.9 及以上支持 `TimeMessage`」「Plus 版 `pip install wxautox4`，仅支持 Python 3.9–3.13」「微信客户端 4.1」「免费版 wxauto4 最新支持到客户端 4.1.8.107」）
  - 读过的页面（导航「核心概念」下就这 5 个 class 页，另外读了安装 / 快速开始 / 示例 / 常见问题）：
    - https://docs.wxauto.org/docs/class/WeChat.html
    - https://docs.wxauto.org/docs/class/Chat.html
    - https://docs.wxauto.org/docs/class/Message.html
    - https://docs.wxauto.org/docs/class/Moment.html
    - https://docs.wxauto.org/docs/class/Other.html （WxResponse / WxParam / WeChatImage）
    - https://docs.wxauto.org/docs/install.html 、/docs/start.html 、/docs/example.html 、/docs/issues.html
  - 文档里方法名前的「✨」= Plus 版（wxautox4）专用，免费版 wxauto4 没有。本项目用的是 Plus 版，所以全部可用。
- **本机装的包**：`wxautox4 41.1.1.post1`，路径 `C:\Users\Admin\AppData\Local\Python\pythoncore-3.12-64\Lib\site-packages\wxautox4`（win-shukong，Python 3.12.8，微信客户端 4.1.9.35）。
  核对方式：`ssh win-shukong` 后用 `inspect` 打印各类的签名 / docstring / 类属性（没有实例化 `WeChat()`，没碰 UI）。
  注意包里**大部分模块是编译的 `.pyd`**（`wx`、`msgs/*`、`ui/chatbox|component|main|moment|navigationbox|sessionbox`、`utils/tools|useful|win32`、`uia/uiplug`），
  只有 `param.py`、`exceptions.py`、`logger.py`、`ui/base.py`、`uia/uiautomation.py`、`utils/lock.py` 是源码。
  所以**实例属性**（如 `msg.content`、`msg.sender`、`msg.id`、`session.info`、`new.name`）inspect 看不到，这类只能以文档为准。
- **本项目**：`wxbot_core.py` 与 `plugins/` 里的实际调用（grep），见第 12 节。

---

## 0. 包结构与导入

`wxautox4` 顶层 `__all__ = ['WeChat', 'Chat', 'LoginWnd', 'WxParam', 'authenticate']`；顶层还能拿到 `msgs`、`param`、`ui`、`uia`、`utils`、`exceptions`、`logger`、`languages` 子模块（`__getattr__` 懒加载）。

```python
from wxautox4 import WeChat, Chat, LoginWnd, WxParam
from wxautox4.msgs import *          # 各消息类（FriendMessage / TimeMessage ...）
from wxautox4.param import WxParam, WxResponse
from wxautox4.ui.component import WeChatImage, WeChatDialog
```

⚠️ **循环导入坑（本机实测）**：在还没 import 过 `WeChat` 的进程里直接 `from wxautox4 import msgs` 会报
`ImportError: cannot import name 'BaseMessage' from 'wxautox4.msgs.base'`（`msgs/base` → `ui` → `chatbox` → `msgs/msg` → `mattr` 转了一圈）。
先 `from wxautox4 import WeChat` 再拿 `msgs` 就正常。`wxbot_core.py` 第 47–49 行的顺序（先 `WeChat` 后 `msgs`）恰好是对的，别调换。

子模块一览（`pkgutil.walk_packages`）：`cli.main / cli.worker`（命令行）、`exceptions`、`languages`、`logger`、`msgs`、`param`、
`ui.base / chatbox / component / main / moment / navigationbox / sessionbox`、`uia.uiautomation / uiplug`、`utils.lock / tools / useful / win32`、`wx`。

---

## 1. WeChat 类（主窗口）

`wxautox4.wx.WeChat`，MRO：`WeChat → Chat → Listener → ABC`。**继承了 Chat 类的全部方法**（SendMsg / SendFiles / ChatInfo / GetAllMessage / AddGroupMembers / SetGroupXxx …见第 2 节），下面只列 WeChat 自己的。
来源：https://docs.wxauto.org/docs/class/WeChat.html

### 1.1 初始化

文档：

| 参数 | 类型 | 默认值 | 描述 |
|---|---|---|---|
| resize | bool | True | 是否自动调整窗口尺寸 |
| debug | bool | False | 是否开启调试模式 |
| version | Literal['微信','WeChat'] | '微信' | 客户端版本，'WeChat' 支持国际版 |

⚠️ 本机 inspect 签名：`WeChat(nickname: str = None, start_listener: bool = True, debug: bool = False, resize: bool = True, version: Literal['微信','WeChat'] = '微信', **kwargs)`
—— 多出 `nickname`（多开时指定登录昵称）和 `start_listener`（是否自动起监听线程）两个未在文档里出现的参数，含义只有名字可推，文档没写就别依赖。
本项目：`self.wx = WeChat(version='微信')`，失败回退 `WeChat(version='WeChat')`（`wxbot_core.py` 2767/2771 行）。
另有 `WeChat` 的只读属性 `dir`、`path`（inspect 可见，文档未提）。

⚠️ **41.x 有实例缓存**：`WeChatMainWnd` 上有静态方法 `clear_cache(nickname=None)`、`set_cache_expiry(seconds)`（默认 3600 秒），
且初始化日志会打「初始化成功，获取到已登录窗口：🐶肥肉（使用缓存）」——这句带 emoji 的日志正是 CLAUDE.md 3.14 里 GBK stdout 崩溃的来源。
`wxbot_core.py` 2562 行注释也提到「`WeChat()` 走缓存复活同一实例，上一轮的注册会留下来」。

### 1.2 保持运行 KeepRunning()
纯监听程序主线程会退出，调这个挂住。返回无。本项目主循环自己轮询，没用它。

### 1.3 获取当前会话列表 GetSession() → `List[SessionElement]`
文档示例 `session.info`。本机 inspect `SessionElement(control, parent)` 的方法：`click()` / `double_click()` / `right_click()` / `roll_into_view()` / `select_option(option, wait=0.3)`；`info` 是实例属性看不到。
本项目：`plugins/ncc_community/forward.py:2006`。

### 1.4 打开聊天窗口 ChatWith(who, exact)
文档：`who: str` 必填；`exact: bool = False` 搜索好友时是否精确匹配；**返回值：无**。

⚠️ 本机 inspect：`ChatWith(who: str, exact: bool = True, force: bool = False, force_wait: float|int = 0.5)`
- `exact` **默认 True**（文档写 False）；
- 多出 `force`（不论是否匹配到都强制切换，启用后 exact 无效；原理是输入关键字等 `force_wait` 秒后不判断结果直接回车，docstring 自己都写「谨慎使用」）和 `force_wait`。
- ⚠️ **返回值不是「无」**：本项目实测（CLAUDE.md 3.6）找不到会话时**静默返回 falsy 的 WxResponse、不抛异常、窗口留在原处**。不接返回值就继续操作 = 在上一个窗口上干活（2026-07-29/30 拉群事故）。
  项目里统一走 `forward._switched()`：接返回值 + `ChatInfo()` 复核当前窗口，两道都过才动手。
- 找不到会话时走搜索框，只等 `WxParam.SEARCH_CHAT_TIMEOUT`（默认 2 秒，本项目调成 5）。
- 搜索是**子串匹配**且优先匹配备注（CLAUDE.md 3.6「打备注放行错打」一案）。

### 1.5 子窗口 GetSubWindow(nickname) → `Chat`；GetAllSubWindow() → `List[Chat]`
本项目：`wxbot_core.py:2599/2615`、`plugins/listen_health/probe.py:387/406`、`plugins/ai_news_note/sender.py:1024`（发日报优先用已开着的子窗口，不新建顶层窗口）。

### 1.6 添加监听 AddListenChat(nickname, callback)
文档：`nickname: str` 必填；`callback: Callable[[Message, Chat], None]` 必填，参数 `(Message对象, Chat对象)`。
返回：成功 → `Chat`（子窗口实例）；失败 → `WxResponse`。
⚠️ 本机 inspect 返回注解写的是 `-> WxResponse`，与文档「成功返回 Chat」不一致；本项目按「truthy 即成功，再用 `GetSubWindow` 复核」处理（`probe.py:399-406`、`diag_open_window.py:313-314`）。
本项目：`wxbot_core.py:2626/4512/4547`，回调是 `self.message_handle_callback(msg, chat)`。
**MoveWindow 1400 就是这个方法报的**（窗口没弹出来，wxautox 拿到 0 句柄），全部处置见 CLAUDE.md 3.18；`plugins/listen_health/tap.py` 把它包了一层（指针守卫 + 录像）。

### 1.7 移除监听 RemoveListenChat(nickname) → `WxResponse`
⚠️ 本机 inspect 多一个 `close_window: bool = True`（是否顺手关掉子窗口），文档未列。本项目 5 处调用都只传 nickname。

### 1.8 StartListening() / StopListening(remove=True)
`StopListening(remove)`：`remove` 是否移出所有子窗口。本项目在切换模式 / 退出时调（`wxbot_core.py:2801/2803/5299/5377`）。

### 1.9 SwitchToChat() / SwitchToContact()
切到聊天页 / 联系人页，返回无。本项目在处理好友申请前后切页（`wxbot_core.py:4924-4938`）。

### 1.10 朋友圈 Moments(timeout=3) → `MomentsWnd`；PublishMoment(text, media_files, privacy_config)
`PublishMoment` 本机签名 `(text=None, media_files: List[str]=None, privacy_config: PrivacyConfig=None, wait_upload: float=3)` → `WxResponse`，多一个 `wait_upload`（等图片/视频上传的秒数）。
`privacy_config` 是 dict：`{'privacy': '公开'|'白名单'|'黑名单', 'tags': [...], 'friends': [...]}`（`PrivacyConfig` 类默认 `privacy='公开', friends=[], tags=[]`；文档只示范了 privacy+tags）。
详见第 7 节。本项目：`wxbot_core.py:3059/3117`（自己 `pyq.Publish` / `GetMoments` / `Like`）。

### 1.11 好友申请 GetNewFriends(acceptable=True, roll_times=0) → `List[NewFriendElement]`
`acceptable` 是否过滤掉已接受的申请；`roll_times` 向下滚动次数（加载更多，一般用不到）。元素用法见第 8 节。
本项目：`wxbot_core.py:4915`，之后 `new.accept(remark=..., tags=...)`。

### 1.12 添加好友 AddNewFriend(keywords, addmsg=None, remark=None, tags=None, permission='朋友圈', timeout=5) → `WxResponse`
文档表格里 `timeout` 那一行写坏了（「int | float | 5」），本机签名 `timeout: int = 5`。文档站把它标为「高风险接口」。本项目没用。

### 1.13 修改好友信息 EditFriendInfo(add_tags=None, remove_tags=None, remark=None, tag_wait=0.2) → `WxResponse`
三个参数不能同时为 None。**实际定义在 Chat 类上**（inspect `[in Chat]`），主窗口 / 子窗口都能调。
⚠️ 本项目实测（CLAUDE.md 3.6）：**对群聊直接拒绝**，返回 `{'status':'失败','message':'该方法只适用于好友页面'}`，别拿它清群备注。
本项目：`plugins/ncc_community/forward.py:2065`（诊断指令）。

### 1.14 全局监听 GetNextNewMessage(filter_mute=False, callback=None) → `Dict`
文档：`filter_mute` 是否过滤免打扰；`callback: Callable[[Message], None]`，**不要在回调里发消息**（会重置消息窗口，收不全）。
返回值示例：
```python
{'chat_name': 'wxauto交流', 'chat_type': 'group', 'remark': '群备注名',  # remark 仅有备注时存在
 'msg': [<TimeMessage>, <FriendImageMessage>, <FriendTextMessage>, ...]}
```
（文档「类型」栏写的是 `Dict[str, List[Message]]`，示例却是上面这个带 chat_name/chat_type/msg 的结构——以示例为准，本项目 5148 行后的解析也是按 `chat_name`/`chat_type`/`msg` 取的。）
⚠️ 本机 inspect 多一个 `timeout=None`。受 `WxParam.GET_NEXT_MAX_QUANTITY`(30) / `GET_NEXT_MAX_RUNTIME`(10 秒) 限制。
本项目：`wxbot_core.py:5059/5148`（全局模式；回调里下载图片/转语音，把结果按 `msg.id` 暂存再回填 `msg.content`）。

### 1.15 最近群聊 GetAllRecentGroups()
文档：返回 `WxResponse | List[str]`（失败返回 WxResponse）。
⚠️ 本机 inspect：`GetAllRecentGroups(speed: int = 1, interval: float = 0.1, timeout=None)`，docstring 返回 `List[Tuple]`；
本项目实测（CLAUDE.md 3.6）返回 **`List[Tuple[str, int]]` = (会话显示名, 成员数)**，显示名约 16 字会截断，且只覆盖「最近」会话（每轮 95–102 个不等，不是全量）。
本项目：`plugins/ncc_community/forward.py:1239/1553/2116`。

### 1.16 发送链接卡片 SendUrlCard(url, friends=None, message=None, timeout=10) → `WxResponse`
⚠️ 本机签名 `friends: Union[str, List[str]]` **没有默认值**（文档写默认 None）。本项目：`plugins/ncc_community/welcome.py:65`。

### 1.17 历史消息 GetHistoryMessage(n, callback=None, interval=0.2, speed=1, goback=True) → `List[Message]`
先 `ChatWith` 到目标会话再调。`callback` 返回 `WxParam.CALLBACK_STOP_SIGN`（='stop'）即停止（v40.1.8+），可配合 `isinstance(msg, TimeMessage) and msg.time < '2026-02-01'` 按时间停（v40.1.9+）。`goback` 取完是否滚回最新消息。
⚠️ 本机多一个 `timeout=None`（最大读取秒数）。本项目：`plugins/ncc_community/forward.py:170/172/526/530`（找转发源消息，回调里返回 `WxParam.CALLBACK_STOP_SIGN`，见 504-505 行）。

### 1.18 好友详情 GetFriendDetails(n=None, timeout=0xFFFFF, save_head_image=False, save_head_wait=0, interval=0, callback=None, speed=3, max_repeat=10) → `List[dict]`
每个好友约 0.3–0.5 秒，好友多先用小 n 试。返回 dict 键：`头像 / 昵称 / 微信号 / 标签 / 共同群聊 / 个性签名 / 来源`。
⚠️ `callback` 文档是 `Callable[[str], bool]`（参数为好友昵称，返回 True 表示从这里开始取），而本机 docstring 写「参数为好友详情字典，返回 True 继续、False 停止」——两边语义冲突，用之前先拿 n=3 试一下到底传什么。本项目没用。

### 1.19 创建群聊 CreateGroup(contacts: List[str]) → `WxResponse`
本项目没用。

### 1.20 IsOnline() → bool；GetMyInfo() → Dict[str, str]
`GetMyInfo` 本项目实测（41.x）返回 `{"display_name": "🐶肥肉", "id": "FeiRou_NCC"}`，`id` 用作记忆目录名（`wxbot_core.py:2790`）。

### 1.21 对话框 GetDialog(wait=3) → `WeChatDialog`
定义在 Chat 类上。`WeChatDialog` 方法：`click_button(text, move=True)`、`get_all_text()`、属性 `control`。本项目没用（`plugins/ai_news_note` 自己用 UIA 处理弹窗）。

### 1.22 文档没写、本机 inspect 有的 WeChat 方法（用之前自己测）
- `GetTagContacts(tag, speed=3, interval=0.1, wait=1, timeout=None)` —— 按标签取联系人（底层 `ContactManageWnd`）。
- `ShutDown()` —— 无 docstring，**多半是关微信进程，绝对别在生产上调**（微信登录必须人在屏幕点，CLAUDE.md 3.8）。
- `GetNewMessage() -> List[Message]`（Chat 类）—— 当前窗口新消息。
- `LoadMoreCache(load_times=1)`（Chat 类）—— 无 docstring。
- ⚠️ **没有 `GetListenMessage`**：`wxbot_core.py:4950`（`listen_mode()`）和 `5085`（注释标「旧版，当前未启用」的 `process_listen_messages()`）还在调它，41.1.1.post1 的 WeChat 类 inspect 里不存在这个方法，grep 确认这两个函数**目前没有任何调用方**（5220 行注释「已注释备用」），是死代码；真要启用会 `AttributeError`。现役路径是 AddListenChat 回调 + GetNextNewMessage。

---

## 2. Chat 类（独立聊天子窗口）

`wxautox4.wx.Chat(core: WeChatSubWnd = None)`。文档解释了为什么要这个类：wxauto 只能拿人眼可见的 UI，要监听某人就得把他的聊天窗口独立出来，UI 元素不会因主窗口切换而丢，也不用每条消息都切窗口。
来源：https://docs.wxauto.org/docs/class/Chat.html

属性：`who: str`（子窗口聊天对象名）、`chat_type: str`（`friend` 好友 / `group` 群聊 / `service` 客服 / `official` 公众号）。
⚠️ 本项目 `MainWindowChat` 回落对象曾少了 `chat_type` 导致丢消息（CLAUDE.md 3.18），凡是冒充 Chat 的对象都得把 `who`、`chat_type`、`SendMsg` 备齐（`tests/test_main_window_chat.py` 会扫）。

| 方法 | 签名（本机 inspect） | 返回 | 备注 |
|---|---|---|---|
| Show() | `Show()` | 无 | 显示窗口。本项目 `forward.py:1745/1808` |
| ChatInfo() | `ChatInfo() -> Dict[str,str]` | dict | 文档示例：好友 `{'chat_type':'friend','chat_name':'张三'}`；群 `{'group_member_count':500,'chat_type':'group','chat_name':'工作群'}`；客服多 `company`；公众号只有 chat_type/chat_name。⚠️ 本项目实测群聊**只有这三个键、没有 remark**，`chat_name` 是**当前显示名**（有备注就是备注），微信侧无从知道真名（CLAUDE.md 3.6） |
| AtAll(msg, who=None, exact=False) | 同 | WxResponse | @所有人 |
| SendMsg(msg, who=None, clear=True, at=None, exact=False) | Chat 版同文档；⚠️ **WeChat 版多 `max_retries: int = 3`**（who 不为空时切窗口重试次数，避免发错对象） | WxResponse | `who`/`exact` **在子窗口上无效**；`at: str|List[str]`。本项目用得最多（102 处） |
| SendFiles(filepath, who=None, exact=False) | WeChat 版同样多 `max_retries=3` | WxResponse | `filepath: str|list` 绝对路径。受 `WxParam.SEND_FILE_TIMEOUT` |
| SendAudio(filepath, duration=None, start=0, who=None, exact=False) | WeChat 版多 `max_retries=3` | WxResponse | **Beta**，需客户端 4.1.9+ 及额外配置（`WxParam.AUDIO_PARAM`，VB-CABLE 虚拟声卡） |
| GetAllMessage() | `-> List[Message]` | list | 当前窗口所有可见消息。本项目 `wxbot_core.py:5009`、`forward.py:175/533/537` |
| AddGroupMembers(members=None) | `members: str|List[str]` | WxResponse | 先 `ChatWith` 切到群再调（3.9 版的 group 参数没了）。⚠️ 本项目血泪：切群失败后照样调它，会在残留的私聊窗口上点「添加成员」，报误导性的「未选择任何新增成员」，万一选中了人还会**新建一个群**（CLAUDE.md 3.6）。`invite.py:196` |
| SetGroupName(value) | 同 | WxResponse | |
| SetGroupRemark(value) | 同 | WxResponse | ⚠️ 本项目实测：对已有备注是**追加**、空串清不掉、上限 48 字节超了截断且重跑再追加一截；`wxresponse_ok` 连 None 都判成功所以要**回读复核**（`remark.verify_remark`）。`batch.py:126`、`remark.py:131`、`forward.py:2189` |
| SetGroupAnnouncement(value) | 同 | WxResponse | |
| SetGroupMyNickname(value) | 同 | WxResponse | |
| Close() | 同 | None | 关闭窗口 |
| EditFriendInfo(...) / GetDialog(wait=3) / GetNewMessage() / LoadMoreCache(load_times=1) | 见 1.13 / 1.21 / 1.22 | | 定义在 Chat 上，文档放在 WeChat 页或没写 |

底层：`Chat.core` 是 `wxautox4.ui.main.WeChatSubWnd(key, parent, timeout=3, resize=False)`，其上是小写风格的 `send_msg / send_files / chat_info / get_msgs / get_new_msgs / load_more_message(load_times=1, who=None, exact=False) / is_online / set_window_size(width, height, location=None) / auto_resize / exists(wait=0) / close`，
`WeChatMainWnd` 另有 `switch_chat(keywords, exact=True, force=False, force_wait=0.5)`、`open_separate_window(keywords, resize=False)`、`get_sub_wnd(who)`、`get_all_sub_wnds()`、`safe_send_msg/safe_send_files/safe_send_audio(..., max_retries=3)`、`get_recent_groups(speed, interval, timeout=None)`、`get_contact_info(save_head_image=False, save_head_wait=0)`。
这些是内部 API，文档不保证，本项目只有诊断脚本碰过（`ChatMoreInfoWnd(wx.ChatBox)` 那次没走通，CLAUDE.md 3.6）。

---

## 3. 消息类 `wxautox4.msgs`

来源：https://docs.wxauto.org/docs/class/Message.html

### 3.1 两个固定属性
- `attr` 消息来源：`system` 系统 / `self` 自己 / `friend` 好友 / `other` 其他。⚠️ 本机 inspect 还有 `official`（`OfficialMessage`，公众号消息）；`HumanMessage` 基类的 `attr='human'`（文档表格把它写成 friend，是文档笔误，实际子类各自覆盖）。
- `type` 内容类型：`time / text / quote / voice / image / video / file / location / link / emotion / merge / personal_card / note / other`。⚠️ 本机多 `miniapp`（小程序卡片，`MiniAppMessage`）、`official`、`system`（`SystemMessage.type='system'`）、`base`。
  本项目 `wxbot_core.py:3934` 的转发类型清单已经把 `miniapp` 算进去了。

### 3.2 类层级（本机 inspect，与文档一致的部分）

```
Message (wxautox4.msgs.base.Message)
└─ BaseMessage(control, parent: ChatBox)   type='base' attr='base'   方法: exists() / roll_into_view() / 属性 raw
   ├─ SystemMessage        attr='system' type='system'
   │  └─ TimeMessage       type='time'   (__init__ 多一个 time_str='')  属性 time: 'yyyy-mm-dd HH:MM:SS'
   ├─ OfficialMessage      attr='official' type='official'  get_content(retry=10, interval=0.1) / right_click() / select_option(option, timeout=2, interval=0.1)
   ├─ HumanMessage         attr='human'  见 3.3
   │  ├─ FriendMessage     attr='friend'  见 3.4
   │  │  └─ FriendTextMessage / FriendQuoteMessage / FriendVoiceMessage / FriendImageMessage / FriendVideoMessage
   │  │     / FriendFileMessage / FriendLocationMessage / FriendLinkMessage / FriendEmotionMessage / FriendMergeMessage
   │  │     / FriendPersonalCardMessage / FriendNoteMessage / FriendMiniAppMessage / FriendOtherMessage
   │  └─ SelfMessage       attr='self'    同样 14 个 SelfXxxMessage
   └─ 内容类型 mixin（各 FriendXxx/SelfXxx 同时继承它）：
      TextMessage / QuoteMessage / VoiceMessage / ImageMessage / VideoMessage / FileMessage / LocationMessage
      / LinkMessage / EmotionMessage / MergeMessage / PersonalCardMessage / NoteMessage / MiniAppMessage / OtherMessage
NotExistsMessage (占位类，无方法)
```
例：`FriendImageMessage` 的 MRO = `FriendMessage → HumanMessage → ImageMessage → BaseMessage`。所以 `isinstance(msg, FriendMessage)` 和 `isinstance(msg, ImageMessage)` 都成立。

### 3.3 所有消息共有的属性 / 方法（Message 基类）

| 属性 | 类型 | 描述 |
|---|---|---|
| type | str | 内容类型 |
| attr | str | 来源类型 |
| info | Dict | 消息详细信息 |
| id | str | 消息 UI ID（不重复，切换 UI 后会变） |
| ✨hash | str | 消息 hash（可能重复，切换 UI 后不变；要开 `WxParam.MESSAGE_HASH=True`，本项目开了） |
| sender | str | 发送者（群消息里是显示名） |
| content | str | 消息内容 |

方法：`chat_info()` → dict（同 `Chat.ChatInfo()` 格式）；`roll_into_view()` 滚到视野内。
⚠️ 本机 inspect 在 `BaseMessage` 上只看到 `exists()`、`roll_into_view()`、`raw`，**没有 `chat_info`**（可能是实例上动态绑定的，编译模块看不到），用之前 `hasattr` 一下。
本项目直接读 `msg.attr / type / sender / content / id`，并**回写 `msg.content`**（图片下载后换成路径、语音换成转写文本，`wxbot_core.py:3437-3451`、`5160-5168`）。

### 3.4 HumanMessage（人发的消息：self + friend）

特有属性：`sender`（群消息中显示名）。方法（本机 inspect 签名）：

| 方法 | 签名 | 返回 | 文档说明 |
|---|---|---|---|
| click() | `click()` | | 点击该消息，图片/视频等特殊消息才有用 |
| select_option(option) | `select_option(option: str, timeout=2)` | WxResponse | 右键弹菜单选指定项，如 `"复制"` |
| quote(text, at=None, timeout=3) | `quote(text: str, at: List[str]|str = None, timeout: int = 3)` | WxResponse | 引用并回复。本项目 `wxbot_core.py:3769/3771/3796`（群聊「引用回复」开关） |
| forward(targets, message=None, timeout=3) | `forward(targets: List[str]|str, message: str = None, timeout: int = 3, interval: float = 0.1)` | WxResponse | 转发；`message` 附加消息仅 Plus 有效；⚠️ 本机多 `interval`（选联系人间隔）。⚠️ 本项目实测 40.1.15 **成功时返回 None**（与文档 WxResponse 不符），`forward.py` 按此处理，升级后留意；一次只传一个群，绕开微信「多选转发 ≤9」上限。本项目 `wxbot_core.py:3935/3940` |
| tickle() | `tickle()` | WxResponse | 拍一拍发送人 |
| ✨download_head_image() | `download_head_image()` | | 下载发送人头像 |
| ✨edit_info(add_tags=None, remove_tags=None, remark=None) | `edit_info(add_tags=None, remove_tags=None, remark=None, tag_wait=0.2)` | WxResponse | 编辑发送人备注/标签，三参不能全 None |
| （文档未列）reply(text, at=None) | `reply(text: str, at: List[str]|str = None)` | WxResponse | 回复消息（不引用）。文档只在示例里用了 `msg.reply('收到')` |
| （文档未列）right_click() / click_head(pos='left') / delete(timeout=3, force_move=False) | | | 右键 / 点头像 / 删除该消息。无 docstring，别在生产用 |

### 3.5 FriendMessage（对方发的）
继承 HumanMessage，另有：
- `sender_info()` → Dict：发送人信息。
- ✨`delete_friend(clear=True)` → WxResponse：删除该联系人，`clear` 是否同时清聊天记录。
- ✨`add_friend(addmsg=None, remark=None, tags=None, permission='朋友圈')` → WxResponse：把群里尚未添加的成员加为好友（文档标「高风险接口」）。
- （文档未列）`at(content: str, quote: bool = False)` → WxResponse：@该消息发送人并发内容，`quote=True` 顺便引用。

### 3.6 SelfMessage（自己发的）
`attr='self'`，无额外方法。本项目用它识别机器人自己的发言（`wxbot_core.py:3507`）并靠 🤖 前缀防自触发。

### 3.7 各内容类型的特有属性 / 方法

| 类 | type | 特有属性 | 方法（本机 inspect 签名 / 文档） | ⚠️ 差异 |
|---|---|---|---|---|
| TextMessage | text | | `get_content(wait=0.1)`（文档未列） | |
| QuoteMessage | quote | `quote_content`（被引用内容）、`quote_nickname`（被引用人昵称） | ✨`download_quote_image(dir_path=None, timeout=10) -> Path`（引用的是图片/视频才下载，否则返回 None）；`click_quote()`（文档未列） | 类属性 `repattern = '^(.*)\s*引用\s+(.+?)\s+的消息\s*:\s*(.*)$'` 就是它拆 content 用的正则。本项目 `attach_quote_text()`（`wxbot_core.py:253-265`）把 quote_nickname/quote_content 拼回正文 |
| VoiceMessage | voice | | `to_text() -> str` 语音转文字 | 本项目 `wxbot_core.py:3449/5136` |
| ImageMessage | image | | `download(dir_path=None, original=False)` → Path 成功 / WxResponse 失败；`ocr(timeout=3)` | docstring 说 `ocr` 返回 `OcrResult` 对象（含文字和位置），文档说返回 text。本项目 `msg.download()`（`3435/5123`） |
| VideoMessage | video | | `download(dir_path=None, original=False, timeout=10)` | 类属性 `repattern='视频(\d+):(\d+)'` |
| FileMessage | file | | ✨`download(dir_path=None, force_click=False, timeout=30) -> Path` | ⚠️ 本机 timeout 默认 **30**（文档 10）。`force_click`：自动下载不可用时强制点文件（否则会打开该文件） |
| LocationMessage | location | | 无 | |
| LinkMessage | link | | ✨`get_url()`（文档写 `timeout=10`，本机签名无参数）→ str | |
| EmotionMessage | emotion | | `capture(return_obj=False)`（文档未列，猜是截表情图） | |
| MergeMessage | merge | | `get_messages(speed=3, x=100, timeout=10, wait=0.2)`（文档未列，取合并转发里的所有消息，走 `RecordDetailWindow`） | |
| PersonalCardMessage | personal_card | | ✨`add_friend(addmsg=None, remark=None, tags=None, permission='朋友圈', timeout=3)` | 本机 inspect 在 `PersonalCardMessage` 上**没看到** add_friend（只有 FriendMessage 上的那个），文档写的 `timeout` 参数无从核对 |
| NoteMessage | note | | ✨`get_content(wait=3) -> List[str|Path]`；✨`save_files(dir_path=None, wait=3) -> WxResponse`（成功 data 为文件路径列表）；✨`to_markdown(dir_path=None, wait=3) -> Path` | 本机各多一个 `wait`（笔记加载等待）；受 `WxParam.NOTE_LOAD_TIMEOUT`(30)，超时抛 `WxautoNoteLoadTimeoutError` |
| MiniAppMessage | miniapp | | 无 | 文档未列。本项目美食群里的点评/美团卡片就是它，`dsh_brain` 渲染成 `[大众点评卡片] …` 进历史 |
| OtherMessage | other | | 无 | 暂未支持解析的类型 |

### 3.8 判断消息的推荐写法
```python
if msg.attr == 'friend': ...            # 方法一
if isinstance(msg, FriendMessage): ...   # 方法二
if isinstance(msg, TimeMessage): print(msg.time)
```

---

## 4. 监听机制

来源：WeChat 页 + 示例页 https://docs.wxauto.org/docs/example.html

两条路，本项目两条都在用（面板「监听模式」切换）：

1. **白名单 / 子窗口监听**：`AddListenChat(nickname, callback)` 给每个会话开独立子窗口，wxautox 的守护线程按 `WxParam.LISTEN_INTERVAL`(1 秒) 轮询各子窗口，新消息就调 `callback(msg, chat)`。
   `chat` 是那个子窗口的 `Chat`，可直接 `chat.SendMsg(...)`。`StartListening()/StopListening()` 开关线程，`RemoveListenChat` 撤某个。
   底层是 `ui.chatbox.MessageMonitor(win, anchor_count=3)`：靠「快照对比」找新消息——记上次最后 3 条的 RuntimeId 当锚点，锚点之后的是新消息；处理消息被删除、撤回、RuntimeId 被复用、ListItem↔CheckBox 模式切换（docstring 原文）。
   ⚠️ 线程池大小 `WxParam.LISTENER_EXCUTOR_WORKERS`（默认 4，**文档没写**）。本项目调成 1（CLAUDE.md 3.18 09-06 的分析；09-08 已查出真正根因是真实鼠标指针压在窗口顶边，但保留 1 无害）。
   ⚠️ 4.x 客户端只给「显示出来的部分」注册 UIA 控件，所以子窗口尺寸 `WxParam.CHAT_WINDOW_SIZE` 要拉高（文档默认 (800,6000)，本机包默认 (1200,6000)，本项目 (1500,6000)）。
2. **全局监听**：`GetNextNewMessage(filter_mute, callback)` 在主窗口按未读红点跳到下一个有新消息的会话读一批（上限 `GET_NEXT_MAX_QUANTITY`=30 条 / `GET_NEXT_MAX_RUNTIME`=10 秒），返回 `{'chat_name','chat_type','remark'?,'msg':[...]}`。本项目主循环每 3–5 秒调一次。
   ⚠️ 全局模式下新私聊的首条消息不经过 `message_handle_callback`，本项目在 `get_next_new_message` 后面补了 ncc_community 的拉群关键词入口（CLAUDE.md 3.6）。
   ⚠️ 已被子窗口监听的群，消息不会再从这里冒出来；反之，配置里群名对不上（打🐶后显示名变了）导致子窗口没开成时，日志就是一串「私聊全局监听收到群聊消息，跳过」（CLAUDE.md 3.6 09-06 事故）。

其它：`GetHistoryMessage` 滚动读历史；`Chat.GetAllMessage()` 读当前可见的全部；`GetNewMessage()` 读当前窗口新消息（文档未列）。

---

## 5. WxParam（全局参数）

`wxautox4.param.WxParam`，**在创建 `WeChat()` 之前改类属性**。来源：https://docs.wxauto.org/docs/class/Other.html

| 属性 | 类型 | 文档默认 | 本机 41.1.1.post1 实际默认 | 描述 | 本项目设置（`wxbot_core.py` 85-98 行） |
|---|---|---|---|---|---|
| LANGUAGE | Literal['cn','cn_t','en'] | 'cn' | 'cn' | 简体/繁体/英文 | |
| ENABLE_FILE_LOGGER | bool | True | True | 是否写日志文件（`wxauto_logs/app_YYYYMMDD.log`，ui_watchdog 在 tail 它） | |
| DEFAULT_SAVE_PATH | str | `./wxautox` | ⚠️ `C:\Users\Admin\wxautox文件下载`（import 时按 cwd 拼成绝对路径，目录名也不是文档写的 `wxautox`） | 下载文件/图片默认目录 | |
| ✨MESSAGE_HASH | bool | False | False | 启用消息 hash 辅助去重，稍影响性能 | **True** |
| DEFAULT_MESSAGE_XBIAS | int | 51 | 51 | 头像到消息 X 偏移，定位/点击消息用 | |
| DEFAULT_MESSAGE_YBIAS | int | 30 | 30 | 头像到消息 Y 偏移 | **40** |
| FORCE_MESSAGE_XBIAS | bool | False | False | 每次启动强制重新获取 X 偏移 | **True** |
| LISTEN_INTERVAL | int | 1 | 1 | 监听轮询间隔（秒） | |
| ⚠️ LISTENER_EXCUTOR_WORKERS | int | **文档未列** | 4 | 监听线程池大小 | **1** |
| SEARCH_CHAT_TIMEOUT | int | 2 | 2 | ChatWith 走搜索框的等待秒数 | **5**（`wxbot_core.py` 注释说「文档写 5」，现在文档站写的就是 2） |
| ✨NOTE_LOAD_TIMEOUT | int | 30 | 30 | 笔记加载超时 | |
| SEND_FILE_TIMEOUT | int | 10 | 10 | 发文件超时 | |
| CHAT_WINDOW_SIZE | tuple | (800, 6000) | ⚠️ (1200, 6000) | 监听子窗口尺寸 | **(1500, 6000)** |
| SEND_CONTENT_RATIO | float | 0.9 | 0.9 | 编辑框内容与要发内容的相似度阈值（特殊符号转码可能不 100% 相等），达到才回车 | |
| GET_NEXT_MAX_QUANTITY | int | 30 | 30 | GetNextNewMessage 单次最多条数 | |
| GET_NEXT_MAX_RUNTIME | int | 10 | 10 | GetNextNewMessage 最长秒数 | |
| SPECIAL_SESSION_NAME | list | ['公众号','折叠的聊天','QQ邮箱提醒','服务号'] | 同 | 特殊会话名 | |
| ✨DEFAULT_STICKERS | list | 128 个 `[微笑]`… | 同（列表末尾 `[天啊]` `[强]` `[汗]` `[握手]` 是重复项，文档和包里都这样） | 默认表情，`SendMsg('[微笑][拥抱]')` 直接用 | |
| CALLBACK_STOP_SIGN | str | 'stop' | 'stop' | 回调返回它即停止（GetHistoryMessage 等） | forward.py 用 |
| INPUT_AT_INTERVAL | float | 0.5 | 0.5 | @成员输入间隔 | |
| ⚠️ AUDIO_PARAM | dict | **文档未列** | `{'device_keyword':'CABLE Input','device_id':None,'samplerate':None,'channels':None,'block_frames':1024,'latency':'high','ffmpeg_path':None,'ffprobe_path':None}` | SendAudio 用的虚拟声卡/ffmpeg 配置（`utils.tools.VBCable`） | |

---

## 6. WxResponse（返回结构）

`wxautox4.param.WxResponse(status: str, message: str, data: dict = None)`，**是 dict 的子类**。来源：Other 页。

```python
result = wx.SendMsg(...)
if result:                 # 真值 = 成功
    data = result['data']  # 多数情况下 None
else:
    print(result['message'])
```
本机 inspect：属性 `is_success`；类方法 `success(message=None, data=None)` / `failure(message, data=None)` / `error(message, data=None)`；`to_dict()`。
本项目 `remark.py:22` 的注释：`SetGroupRemark` 返回 `{'status':'成功'|...}`，据此 `status` 键取值是中文「成功/失败」（`EditFriendInfo` 对群返回 `{'status':'失败','message':'该方法只适用于好友页面'}` 也印证了）。
⚠️ 不少方法**成功时返回 None 而不是 WxResponse**（`msg.forward()` 实测），`if result:` 会误判失败——本项目 `wxresponse_ok` 是「None 也算成功」的宽松判法，反过来又让 `SetGroupRemark` 几乎不设防，所以关键操作一律**回读复核**。

---

## 7. 朋友圈

来源：https://docs.wxauto.org/docs/class/Moment.html

`pyq = wx.Moments(timeout=3)` → `MomentsWnd`（返回 None 说明没开朋友圈，要在手机端设置）。

**MomentsWnd**（`wxautox4.ui.moment.MomentsWnd(root, timeout=5)`）：

| 方法 | 文档 | 本机 inspect | ⚠️ |
|---|---|---|---|
| GetMoments(next_page=False, speed1=3, speed2=1) → `List[Moments]` | `next_page` 翻页后再取；`speed1` 翻页滚动速度（建议 3–10）；`speed2` 翻页末尾速度（建议 1–3） | `GetMoments(next_page=False, speed1=1, speed2=1, interval=0.3, force_wait=1, timeout=None)` | speed1 默认 1（文档 3）；多 interval/force_wait/timeout |
| Refresh() | 刷新 | 同 | |
| Close() | 文档标题写 `close`、示例写 `pyq.Close()` | `Close()`（大写）；另继承 `BaseUIWnd.close()` 小写 | 两个都在 |
| Publish(text, media_files=None, privacy_config=None) | 发朋友圈（文档把 privacy_config 描述错写成「是否翻页后再获取」） | `Publish(text=None, media_files=None, privacy_config: PrivacyConfig=None, wait_upload=3)` | 多 wait_upload |
| 属性 toolbar_buttons | 未列 | 有 | |

**Moments**（单条朋友圈，`wxautox4.ui.moment.Moments(control, parent)`）：属性 `content`；`Like(like=True)`（文档返回无，本机注解 `-> WxResponse`）；`Comment(text)`。

**PrivacyConfig**（dict 子类）：`privacy='公开'`（可 '白名单'/'黑名单'）、`friends=[]`、`tags=[]`。白名单=仅这些标签能看，黑名单=屏蔽这些标签。
示例页警告：频繁发朋友圈可能被判过度营销触发风控。
本项目：`wxbot_core.py:3059-3135`（面板「发朋友圈」「自动点赞」功能：`pyq.Publish(...)`、`pyq.GetMoments()`、`moment.Like()`）。

---

## 8. 新好友 NewFriendElement

来源：WeChat 页 GetNewFriends 一节。`wxautox4.ui.component.NewFriendElement(control, parent)`。

- 属性（文档示例）：`name`、`msg`（示例页又写成 `content`，两处不一致，实例属性 inspect 看不到，用之前 `print(vars(new))` 看一眼）。
- `accept(remark=None, tags=None, permission='朋友圈')`：接受申请并设备注/标签，`permission` 可 '朋友圈' / '仅聊天'（本机 docstring）。文档 docstring 示例里写成 `friend.Accept(...)` 大写，**本机实际是小写 `accept`**，本项目 `wxbot_core.py:4922` 用的小写。
- 本机另有 `delete()`、`get_account()`（文档未列）。
- 处理完 `wx.SwitchToChat()` 切回聊天页。
底层窗口类：`AcceptFriendsWnd` / `AddFriendsWnd` / `SearchNewFriendWnd`（`search(keyword)` / `apply(timeout=3)`）/ `TagAndRemark`（`add_tag / remove_tag / set_remark / current_tags`），内部 API。

---

## 9. 登录窗口 LoginWnd

⚠️ **文档站没有这一页**，只从本机 inspect 得到。`wxautox4.wx.LoginWnd(app_path=None, hwnd=None)`（顶层导出），底层 `ui.main.WeChatLoginWnd`：
- `open()` 打开；`login(timeout=10)` 登录（docstring 无，按名字推测是点登录按钮等扫码/确认）；`reopen()`「重新打开」；`close()`「关闭微信」；`exists(wait=0)`；底层还有 `shutdown()`「关闭进程」。
本项目**不用**——微信登录必须人在屏幕点，且任何关微信进程的操作都禁止（CLAUDE.md 3.8）。

---

## 10. 其它工具类

### 10.1 WeChatImage（图片/视频窗口）
来源：Other 页。文档：`from wxautox4.ui.component import WeChatImage; imgwnd = WeChatImage()`；`save(dir_path=None, timeout=10) -> Path`；`close()`。
⚠️ 本机：构造函数 `WeChatImage(parent)` **需要 parent**（文档示例无参）；`save(dir_path=None, timeout=10, original=False)` 多 `original`；另有 `ocr(timeout=3)`、`load_original()`、`init()`。`ImageMessage.download()` 内部就是走它。

### 10.2 WeChatDialog（弹窗）
`GetDialog(wait=3)` 返回。`click_button(text, move=True)`、`get_all_text()`、属性 `control`。

### 10.3 SessionElement / SessionBox / SearchResultElement
`GetSession()` 返回的元素见 1.3。`SessionBox`（内部）：`get_session()`、`switch_chat(keywords, exact=True, force=False, force_wait=0.5)`、`search(keywords, force=False, force_wait=0.5)`、`open_separate_window(name, force=False)`、`roll_up/roll_down(n=5)`、`go_top()`。
`SearchResultElement`：`click()` / `close()` / `get_all_text()`。

### 10.4 NoteWindow（笔记窗口，内部）
`NoteWindow(timeout=3)`：`parse() -> List[str|Path]`、`save_all(dir_path) -> WxResponse[List[Path]]`、`save_file(file_path, dir_path=None)`、`to_markdown(dir_path=None) -> Path`、`process_dialog()`。`NoteMessage` 的三个方法就是包它。
本项目 `ai_news_note` / `gh_trending_note` 是**发**笔记（自己用 UIA 操作收藏编辑器），wxautox 只提供**读**笔记的能力。

### 10.5 其它内部窗口类（`ui.component`，文档未列，仅供排障时知道有这东西）
`ChatMoreInfoWnd(parent)`（`get_item_control(item)` / `set_group_name` / `set_group_remark` / `set_announcement` / `set_my_nickname`）、`GroupAnnouncement`、`ContactManageWnd(timeout=3)`（`get_tags()` / `select_tag(tag)` / `get_contacts(speed=3, ...)`）、`EditRemarkWindow`、`SelectContactWnd(parent, timeout=2)`（转发/发卡片时的选人框：`search(keyword)` 需完全匹配、`add_message(content)`、`send(target, message=None)`、`confirm()`）、`WeChatBrowser(parent, timeout=3)`（`copy_url` / `send_card` / `select_options`）、`RecordDetailWindow`（合并消息详情）、`Menu(parent, timeout=2)`（`option_names` / `select(item)`）、`ProfileWnd`（`info`）、`UpdateWindow.ignore()`（升级提示）。
`ui.navigationbox.NavigationBox`：`switch_to_chat_page / contact_page / favorites_page / files_page / browser_page`、`open_moments(timeout=3)`、`has_new_message()`、`jump_to_new_session()`。

### 10.6 异常 `wxautox4.exceptions`
`NetWorkError`、`WxautoNoteLoadTimeoutError`、`WxautoOCRError`、`WxautoUINotFoundError`（都直接继承 Exception，无 docstring）。另有 `utils.useful.WxautoLicenseError`、`ui.component._ContactScanTimeout(TimeoutError)`。
⚠️ 本项目最常见的 `MoveWindow 1400` 不是这些，是 `pywintypes.error(1400, 'MoveWindow', '无效的窗口句柄。')` 从 `ui/base.py` 的 `set_window_size()` 里抛出来的（CLAUDE.md 3.18）。

### 10.7 日志 `wxautox4.logger.WxautoLogger`
logger 名 `wxautox4(41.1.1.post1)`；`set_debug(debug=False)` 动态改级别；`setup_file_logger()` 按 `WxParam.ENABLE_FILE_LOGGER` 决定是否写文件。
本项目 `ui_watchdog` 增量 tail `wxauto_logs/app_YYYYMMDD.log` 抓「消息解析失败」（CLAUDE.md 3.13）。

### 10.8 授权 `wxautox4.utils.useful`
`check_license() -> bool`、`authenticate(code)`、`authenticate_with_file(path)`、`offline_auth(code, path)`、`unregister_license()`、`get_licence_file()`、`debug_license()`；命令行 `wxautox4 auth activate 激活码`。
文档 FAQ：一个激活码绑一台电脑不可解绑；一年内可激活、激活后一年更新，过期后已装版本继续能用；**别在沙箱里激活**。
本项目：`web_server.py:1407/1421`（面板上显示激活状态 / 输入激活码），`wxbot_core.py:50`。

### 10.9 UIA 直通 `wxautox4.uia.uiautomation`
就是打包进来的 `uiautomation` 库（`Control`、`WindowControl`、`SetCursorPos` 等）。本项目 `plugins/ai_news_note/sender.py:16` 和 `plugins/ncc_community/forward.py:1525/1660/1835` 直接 `from wxautox4 import uia` 做文档没覆盖的 UI 操作（收藏笔记编辑器、`WindowFromPoint` 之类）。
⚠️ 41.x 的 `Control.DoubleClick(move=False)` 走 `uia.uiplug.Win32.click_by_bbox(bbox, double_click=True, activate=False)`，是**往句柄发消息的假点击**，不是真鼠标——这条是 CLAUDE.md 3.18 一整段调查的起点。

---

## 11. 与文档不一致 / 文档未写的汇总（⚠️ 清单）

| 项 | 文档 | 本机 41.1.1.post1 / 本项目实测 |
|---|---|---|
| `WeChat.__init__` | resize / debug / version | 多 `nickname=None`、`start_listener=True`、`**kwargs` |
| `ChatWith` | exact 默认 False，返回无 | exact 默认 **True**；多 `force`/`force_wait`；找不到时**静默返回 falsy WxResponse** |
| `AddListenChat` 返回 | 成功 Chat / 失败 WxResponse | 注解 `-> WxResponse` |
| `RemoveListenChat` | (nickname) | 多 `close_window=True` |
| `GetNextNewMessage` | (filter_mute, callback) | 多 `timeout=None` |
| `GetHistoryMessage` | 5 个参数 | 多 `timeout=None` |
| `GetAllRecentGroups` | 返回 `List[str]` | 签名 `(speed=1, interval=0.1, timeout=None)`，返回 `List[Tuple[str,int]]`（显示名截断、非全量） |
| `GetFriendDetails.callback` | 参数昵称 str | docstring 说参数是详情 dict、返回 True 继续——冲突 |
| `SendUrlCard.friends` | 默认 None | 必填 |
| `WeChat.SendMsg/SendFiles/SendAudio` | 同 Chat | 多 `max_retries=3` |
| `EditFriendInfo` | 修改好友信息 | 对群聊返回「该方法只适用于好友页面」 |
| `WxParam` | 无 `LISTENER_EXCUTOR_WORKERS`、`AUDIO_PARAM` | 有，默认 4 / 见上表；`CHAT_WINDOW_SIZE` (800,6000)→(1200,6000)；`DEFAULT_SAVE_PATH` `./wxautox`→`<cwd>\wxautox文件下载` |
| `Message.chat_info()` | 有 | BaseMessage inspect 没看到 |
| `HumanMessage.attr` | 'friend' | 'human'（子类各自覆盖） |
| 消息类型 | 14 种 | 多 `miniapp`、`official`、`system` |
| `HumanMessage` 方法 | click/select_option/quote/forward/tickle/download_head_image/edit_info | 另有 `reply`、`right_click`、`click_head`、`delete`；`FriendMessage.at()` |
| `msg.forward()` | 返回 WxResponse | 40.1.15 成功返回 None；多 `interval=0.1` |
| `FileMessage.download.timeout` | 10 | 30 |
| `LinkMessage.get_url` | timeout=10 | 无参数 |
| `NoteMessage` 三方法 | 无 wait | 各多 `wait=3` |
| `ImageMessage.ocr` | 返回 text | docstring 返回 `OcrResult` |
| `MomentsWnd.GetMoments` | speed1=3 | speed1=1，多 interval/force_wait/timeout |
| `Moments.Like` | 返回无 | `-> WxResponse` |
| `NewFriendElement.accept` | docstring 示例 `Accept` | 小写 `accept`；另有 `delete`/`get_account` |
| `WeChatImage()` | 无参构造 | 需要 `parent`；save 多 `original`；另有 `ocr`/`load_original` |
| `LoginWnd` | 文档无此页 | 顶层导出，open/login/reopen/close/exists |
| `GetListenMessage` | 文档无 | 类里也没有，`wxbot_core.py` 4950/5085 两处旧路径调它会 AttributeError |
| `ChatInfo()` 群聊 | 示例三键 | 实测就三键，`chat_name` 是显示名（有备注即备注） |
| `SetGroupRemark` | 修改备注 | 实测追加语义、空串清不掉、48 字节上限 |

---

## 12. 本项目当前用到的 API

按 `grep -rnoE '\b(wx|chat|msg|...)\.[A-Z]\w+\(' wxbot_core.py plugins/` 统计（2026-09-08，排除 `plugins/ncc_community.bak-phase1/` 备份目录），方法名 → 文件。

**WeChat / Chat 类方法**

| 方法 | 文件（次数） |
|---|---|
| SendMsg | wxbot_core.py(102)、ai_news_note/sender.py(3)、ncc_community/common.py(2)、batch.py(2)、welcome.py、invite.py、forward.py、task_runner.py（FileSink 冒充 chat） |
| ChatWith | wxbot_core.py、ncc_community/forward.py(2)、remark.py、invite.py、batch.py |
| ChatInfo | ncc_community/forward.py(5)、remark.py、invite.py |
| AddListenChat | wxbot_core.py(3)、listen_health/probe.py、listen_health/diag_open_window.py(2)；`listen_health/tap.py` 包住 `wx.AddListenChat` |
| RemoveListenChat | wxbot_core.py(5)、listen_health/probe.py、diag_open_window.py(2) |
| StartListening / StopListening | wxbot_core.py(1 / 3) |
| GetSubWindow / GetAllSubWindow | wxbot_core.py、listen_health/probe.py、ai_news_note/sender.py(2)、diag_open_window.py(2) / wxbot_core.py、probe.py |
| GetNextNewMessage | wxbot_core.py(2) |
| GetAllMessage | wxbot_core.py(2)、ncc_community/forward.py(3) |
| GetHistoryMessage | ncc_community/forward.py(4)，回调返回 `WxParam.CALLBACK_STOP_SIGN` |
| GetAllRecentGroups | ncc_community/forward.py(3) |
| GetSession | ncc_community/forward.py |
| SendFiles | wxbot_core.py(2) |
| SendUrlCard | ncc_community/welcome.py |
| AddGroupMembers | ncc_community/invite.py |
| SetGroupRemark | ncc_community/remark.py、batch.py、forward.py |
| EditFriendInfo | ncc_community/forward.py（诊断） |
| Show | wxbot_core.py、ncc_community/forward.py(3) |
| SwitchToChat / SwitchToContact | wxbot_core.py(2 / 1) |
| Moments | wxbot_core.py(2)（随后 `pyq.Publish` / `pyq.GetMoments` / `moment.Like`） |
| GetNewFriends | wxbot_core.py（随后 `new.accept(remark=, tags=)`） |
| IsOnline / GetMyInfo | wxbot_core.py |
| GetListenMessage | wxbot_core.py(2) ⚠️ 41.x 不存在，旧路径 |

**消息对象方法 / 属性**（wxbot_core.py）：`msg.download()`（图片）、`msg.to_text()`（语音）、`message.quote(content, at=message.sender)`、`message.forward(target, message=src_msg)`；读 `attr / type / sender / content / id / quote_content / quote_nickname`，回写 `msg.content`。`plugins/ncc_community/forward.py` 用 `msg.forward(单个群)`。

**WxParam**（wxbot_core.py 85-98 行）：`MESSAGE_HASH=True`、`FORCE_MESSAGE_XBIAS=True`、`CHAT_WINDOW_SIZE=(1500,6000)`、`DEFAULT_MESSAGE_YBIAS=40`、`SEARCH_CHAT_TIMEOUT=5`、`LISTENER_EXCUTOR_WORKERS=1`；`listen_health/diag_open_window.py` 可改 `LISTEN_INTERVAL` / `LISTENER_EXCUTOR_WORKERS` 做 A/B。

**其它**：`wxautox4.utils.useful.check_license / authenticate`（web_server.py、wxbot_core.py）；`wxautox4.uia.uiautomation`（ai_news_note 全家、ncc_community/forward.py）；`from wxautox4.msgs import *`（wxbot_core.py）。

---

## 13. 文档里有、本项目没用但可能有用的能力

- **`msg.reply(text, at=)` / `FriendMessage.at(content, quote=)`**：直接对着消息对象回，省掉 `chat.SendMsg` 前再确认窗口那一步；`at()` 一次搞定「@发送人 + 引用」。
- **`msg.sender_info()`**：拿发送人信息（可能含微信号），签到插件 `build_user_key` 现在只能落到昵称（CLAUDE.md 3.2 的已知风险），值得试一下它给什么。
- **`msg.hash`**（已开 `MESSAGE_HASH`）：切换 UI 后不变的去重键，比 `id` 稳。
- **`QuoteMessage.download_quote_image()`**：引用图片的下载，现在 `wxbot_core.py:3441` 对 quote 类型调的是通用 `download()`。
- **`NoteMessage.get_content / to_markdown`**：读群里别人发的笔记；`MergeMessage.get_messages()` 展开合并转发；`LinkMessage.get_url()` 取链接（美食群收卡片时若是链接卡片可用）。
- **`ImageMessage.ocr()`**：内置 OCR（`WxautoOCRError`），可替代外部识图。
- **`WeChat.AtAll(msg, who)`**：@所有人。
- **`GetDialog().click_button('确定')`**：处理弹窗，比自己用 UIA 找按钮省事。
- **`GetTagContacts(tag)` / `GetFriendDetails(n=)`**：按标签导联系人 / 导好友详情（慢，0.3–0.5 秒一个）。
- **`SetGroupAnnouncement / SetGroupName / SetGroupMyNickname`**：群管理三件套，NCC 社群插件目前只用了 SetGroupRemark。
- **`AddNewFriend(keywords, ...)`**、**`FriendMessage.add_friend()`**、**`delete_friend()`**：加/删好友——文档标「高风险接口」，且用户策略是不批量加人，列出来只是知道有。
- **`SendAudio`**：发语音条（Beta，要 4.1.9+ 客户端 + VB-CABLE 虚拟声卡 + ffmpeg）。
- **`RemoveListenChat(nickname, close_window=False)`**：撤监听但留着窗口，探针场景也许有用。
- **`ChatWith(..., force=True)`**：搜索结果渲染慢时强制回车——docstring 自己都说谨慎，本项目已经有「接返回值 + ChatInfo 复核」的套路，不建议换。
- **`WeChatMainWnd.clear_cache() / set_cache_expiry()`**：41.x 的实例缓存管理，`wxbot_core.py:2562` 提到「WeChat() 走缓存复活同一实例」，重启监听时若被旧注册困扰可以试。
- **`KeepRunning()`**、**`StopListening(remove=False)`**：纯回调模式下的挂机/暂停。
