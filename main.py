"""機器人的社交管家：好友申請、群邀請、被禁言/踢出/設管理的通知。

- 聊過天的熟人加好友：自動通過（每天有上限），通過後打個招呼
- 其他好友申請、所有群邀請：私信主人，主人回「同意 / 拒絕 / 拉黑 [QQ或群號]」
- 被禁言、被踢、被拉進新群、管理員變動：私信主人；禁言期間那個群的消息不再交給模型（反正發不出去）
- 所有事件記進 events.jsonl，給每日彙報用
"""
import asyncio
import json
import random
import re
import sqlite3
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain

from .lively_common import Settings, T2S

AUTO_MIN_TALKS = 1  # 和機器人直接聊過至少這麼多次，才算熟人
AUTO_DAILY_MAX = 10
PENDING_TTL = 7 * 86400
GREETINGS = [
    "加上啦～以后有事直接戳〈我〉就行",
    "嘿嘿，通过啦，私聊也可以来找〈我〉玩～",
    "〈我〉来啦，以后私下也能聊咯",
]
CMD_RE = re.compile(r"^\s*(同意|拒绝|拉黑)\s*(\d{5,12})?\s*$")


def _now() -> float:
    return time.time()


@register(
    "astrbot_plugin_lively_social",
    "GUCine",
    "好友申請、群邀請、禁言踢出通知，熟人自動通過，其餘私信主人決定",
    "2.0.0",
)
class LivelySocial(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.s = Settings(context, config)
        self.dir = Path(StarTools.get_data_dir("astrbot_plugin_lively_social"))
        self.state_file = self.dir / "state.json"
        self.events_file = self.dir / "events.jsonl"
        self.state = self._load()
        self.bond_db = Path(StarTools.get_data_dir("astrbot_plugin_lively_bond")) / "bond.db"

    # ---------- 存儲 ----------
    def _load(self) -> dict:
        try:
            s = json.loads(self.state_file.read_text("utf-8"))
        except Exception:
            s = {}
        s.setdefault("pending", {})  # key: "friend:QQ" / "group:群號"
        s.setdefault("blacklist", [])
        s.setdefault("muted", {})  # 群號 -> 解除時間（0 = 直到解禁）
        s.setdefault("auto_day", "")
        s.setdefault("auto_count", 0)
        return s

    def _save(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self.state_file)
        except Exception as e:
            logger.error(f"[lively_social] 保存状态失败: {e}")

    def _log(self, kind: str, **data):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.events_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(_now()), "kind": kind, **data}, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"[lively_social] 记录事件失败: {e}")

    # ---------- 工具 ----------
    async def _tell_owner(self, text: str):
        try:
            await self.context.send_message(
                self.s.private_umo(self.s.owner()), MessageChain().message(self.s.owner_text(text))
            )
        except Exception as e:
            logger.error(f"[lively_social] 私信主人失败: {e}")

    def _talks(self, qq: str) -> int:
        try:
            con = sqlite3.connect(f"file:{self.bond_db}?mode=ro", uri=True)
            row = con.execute("select talks from people where qq=?", (qq,)).fetchone()
            con.close()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0

    async def _common_groups(self, bot, qq: str) -> list[str]:
        found = []
        for gid in sorted(self.s.groups()):
            try:
                await bot.get_group_member_info(group_id=int(gid), user_id=int(qq), no_cache=False)
                found.append(gid)
            except Exception:
                pass
        return found

    def _auto_quota_left(self) -> bool:
        day = time.strftime("%Y-%m-%d")
        if self.state["auto_day"] != day:
            self.state["auto_day"], self.state["auto_count"] = day, 0
        return self.state["auto_count"] < AUTO_DAILY_MAX

    def _prune(self):
        cut = _now() - PENDING_TTL
        for k in [k for k, v in self.state["pending"].items() if v.get("ts", 0) < cut]:
            self.state["pending"].pop(k, None)
        for gid, until in list(self.state["muted"].items()):
            if until and until < _now():
                self.state["muted"].pop(gid, None)

    # ---------- 入口：在閘門插件（1000）之前，群邀請事件帶的是新群號，會被閘門攔掉 ----------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=2000)
    async def on_event(self, event: AstrMessageEvent):
        raw = getattr(event.message_obj, "raw_message", None)
        post = raw.get("post_type") if isinstance(raw, dict) else None
        if post == "request":
            event.stop_event()
            await self._on_request(event, raw)
            return
        if post == "notice":
            await self._on_notice(event, raw)
            return
        # 主人在私信裡審批
        if (
            not event.get_group_id()
            and self.s.is_owner(event.get_sender_id())
            and self.state["pending"]
        ):
            m = CMD_RE.match((event.message_str or "").translate(T2S))
            if m:
                event.stop_event()
                await self._owner_decide(event, m.group(1), m.group(2))
                return
        # 禁言中的群：消息不交給模型
        gid = str(event.get_group_id() or "")
        if gid and gid in self.state["muted"]:
            until = self.state["muted"][gid]
            if until == 0 or until > _now():
                event.stop_event()
            else:
                self.state["muted"].pop(gid, None)
                self._save()

    # ---------- 申請 ----------
    async def _on_request(self, event: AstrMessageEvent, raw: dict):
        self._prune()
        bot = getattr(event, "bot", None)
        rtype = raw.get("request_type")
        qq = str(raw.get("user_id") or "")
        flag = raw.get("flag", "")
        comment = (raw.get("comment") or "").strip() or "无"
        nickname = qq
        try:
            info = await bot.get_stranger_info(user_id=int(qq)) or {}
            nickname = info.get("nickname") or qq
        except Exception:
            pass

        if rtype == "friend":
            if qq in self.state["blacklist"]:
                await self._approve(bot, "friend", flag, False)
                self._log("friend_request", qq=qq, name=nickname, result="黑名单拒绝")
                return
            talks = self._talks(qq)
            groups = await self._common_groups(bot, qq) if bot else []
            if self.s.is_owner(qq) or (talks >= AUTO_MIN_TALKS and self._auto_quota_left()):
                if await self._approve(bot, "friend", flag, True):
                    if not self.s.is_owner(qq):
                        self.state["auto_count"] += 1
                    self._save()
                    self._log("friend_request", qq=qq, name=nickname, result="自动通过", talks=talks)
                    await self._tell_owner(
                        f"〈我〉自己通过了一个好友申请～\n{nickname}（{qq}），之前聊过 {talks} 次"
                        + (f"，在群 {'、'.join(groups)}" if groups else "")
                    )
                    await self._greet(qq)
                return
            key = f"friend:{qq}"
            self.state["pending"][key] = {
                "type": "friend", "id": qq, "name": nickname, "flag": flag,
                "comment": comment, "ts": int(_now()), "groups": groups,
            }
            self._save()
            self._log("friend_request", qq=qq, name=nickname, result="等主人决定", talks=talks)
            where = f"在群 {'、'.join(groups)}" if groups else "不在〈我〉待的群里"
            await self._tell_owner(
                f"有人想加〈我〉好友～\n{nickname}（QQ {qq}）\n验证信息：{comment}\n"
                f"{where}，和〈我〉聊过 {talks} 次\n"
                f"回「同意 {qq}」「拒绝 {qq}」或「拉黑 {qq}」"
            )
            return

        if rtype == "group" and raw.get("sub_type") == "invite":
            gid = str(raw.get("group_id") or "")
            gname = gid
            try:
                ginfo = await bot.get_group_info(group_id=int(gid)) or {}
                gname = ginfo.get("group_name") or gid
            except Exception:
                pass
            key = f"group:{gid}"
            self.state["pending"][key] = {
                "type": "group", "id": gid, "name": gname, "flag": flag,
                "inviter": qq, "inviter_name": nickname, "ts": int(_now()),
            }
            self._save()
            self._log("group_invite", group=gid, name=gname, inviter=qq)
            await self._tell_owner(
                f"{nickname}（{qq}）拉〈我〉进群「{gname}」（{gid}）\n"
                f"回「同意 {gid}」或「拒绝 {gid}」\n"
                "（进群后默认不说话，要服务这个群还得另外配置）"
            )

    async def _approve(self, bot, kind: str, flag: str, ok: bool) -> bool:
        try:
            if kind == "friend":
                await bot.set_friend_add_request(flag=flag, approve=ok)
            else:
                await bot.set_group_add_request(flag=flag, sub_type="invite", approve=ok)
            return True
        except Exception as e:
            logger.error(f"[lively_social] 审批失败: {e}")
            return False

    async def _greet(self, qq: str):
        await asyncio.sleep(8)  # 等好友關係生效
        try:
            await self.context.send_message(
                self.s.private_umo(qq), MessageChain().message(self.s.fill(random.choice(GREETINGS)))
            )
        except Exception as e:
            logger.warning(f"[lively_social] 打招呼失败: {e}")

    async def _owner_decide(self, event: AstrMessageEvent, action: str, target: str | None):
        self._prune()
        pending = self.state["pending"]
        if target:
            key = next((k for k in (f"friend:{target}", f"group:{target}") if k in pending), None)
        elif len(pending) == 1:
            key = next(iter(pending))
        else:
            lst = "\n".join(f"- {v['name']}（{v['id']}）" for v in pending.values())
            await event.send(MessageChain().message(self.s.owner_text(f"现在有好几个在等，要带上号码哦\n{lst}")))
            return
        if not key:
            await event.send(MessageChain().message(self.s.owner_text("没找到这个申请，可能已经处理过或者过期了")))
            return
        req = pending.pop(key)
        ok = action == "同意"
        done = await self._approve(event.bot, req["type"], req["flag"], ok)
        if action == "拉黑" and req["type"] == "friend" and req["id"] not in self.state["blacklist"]:
            self.state["blacklist"].append(req["id"])
        self._save()
        self._log(f"{req['type']}_decided", id=req["id"], name=req["name"], action=action, done=done)
        if not done:
            await event.send(MessageChain().message(self.s.owner_text("操作没成功，申请可能已经过期了")))
            return
        what = "好友" if req["type"] == "friend" else "群邀请"
        await event.send(MessageChain().message(self.s.owner_text(f"好的，{action}了 {req['name']} 的{what}～")))
        if ok and req["type"] == "friend":
            await self._greet(req["id"])

    # ---------- 通知 ----------
    async def _on_notice(self, event: AstrMessageEvent, raw: dict):
        ntype = raw.get("notice_type")
        sub = raw.get("sub_type")
        self_id = str(raw.get("self_id") or event.get_self_id() or "")
        uid = str(raw.get("user_id") or "")
        gid = str(raw.get("group_id") or "")
        op = str(raw.get("operator_id") or "")
        if ntype == "group_ban" and uid in (self_id, "0"):
            whole = uid == "0"
            if sub == "ban":
                dur = int(raw.get("duration") or 0)
                self.state["muted"][gid] = 0 if whole or dur <= 0 else int(_now()) + dur
                self._save()
                self._log("muted", group=gid, operator=op, duration=dur, whole=whole)
                how = "开了全员禁言" if whole else f"被 {op} 禁言了 {max(1, dur // 60)} 分钟"
                await self._tell_owner(f"呜…〈我〉在群 {gid} {how}，这段时间那边的消息先不回了")
            elif sub == "lift_ban":
                self.state["muted"].pop(gid, None)
                self._save()
                self._log("unmuted", group=gid, operator=op, whole=whole)
                await self._tell_owner(f"群 {gid} 解禁啦，〈我〉又能说话了～")
        elif ntype == "group_decrease" and sub == "kick_me":
            self.state["muted"].pop(gid, None)
            self._save()
            self._log("kicked", group=gid, operator=op)
            await self._tell_owner(f"〈我〉被 {op} 踢出了群 {gid}……")
        elif ntype == "group_increase" and uid == self_id:
            self._log("joined", group=gid, operator=op)
            await self._tell_owner(
                f"〈我〉进了新群 {gid}（{op} 拉的）\n现在默认不在那边说话，要服务这个群跟我说"
            )
        elif ntype == "group_admin" and uid == self_id:
            self._log("admin_" + str(sub), group=gid)
            await self._tell_owner(
                f"〈我〉在群 {gid} {'被设成管理员啦' if sub == 'set' else '的管理员被取消了'}"
            )
        elif ntype == "friend_add":
            self._log("friend_added", qq=uid)
