from mercury._api import LoginRequiredError, ServiceError
from mercury._messenger import MessengerService
from mercury import _store as store
from mercury import _util as util


class ClientError(Exception):
    pass


class Server:
    def __init__(self, send_msg):
        self.send_msg = send_msg
        self.service = MessengerService()
        self.logged_in = False
        session = store.get_session("messenger")
        if session is not None:
            try:
                self.service.restore_session(session)
                self.logged_in = True
            except (LoginRequiredError, ServiceError):
                # We don't have a message to respond to, so just let
                # it go. Not a big deal.
                pass

    def _get_aid(self, data):
        aid = data.get("aid")
        if not isinstance(aid, str):
            raise ClientError("account ID missing or not a string")
        if aid != "messenger":
            raise ClientError("no account with ID: {}".format(aid))
        return aid

    def _convert_conversations(self, service_conversations):
        """
        Take a list of conversations as returned by a Mercury backend, run
        sanity checks, and convert it into the format used in the
        persistent data store (except with an extra "timestamp" key on
        the conversations). Return a 2-tuple of a map as used in the
        "conversations" key and a map from user IDs to names.
        """
        conversations = {}
        if not util.is_sorted(service_conversations, key=lambda c: -c["timestamp"]):
            raise ServiceError(
                "conversations returned out of order: {}",
                [c["timestamp"] for c in service_conversations],
            )
        for sc in service_conversations:
            if sc["id"] in conversations:
                raise ServiceError("duplicate conversation ID {} returned", sc["id"])
            conversations[sc["id"]] = {
                "name": sc["name"],
                "participants": {p["id"] for p in sc["participants"]},
                "timestamp": sc["timestamp"],
            }
        return (
            conversations,
            {
                p["id"]: p["name"]
                for sc in service_conversations
                for p in sc["participants"]
            },
        )

    def _ensure_conversations_loaded(self, account_data, index):
        existing_conversations = account_data["conversations"]
        latest_conversations = self._convert_conversations(
            self.service.get_conversations()
        )
        if not latest_conversations:
            if existing_conversations:
                raise ServiceError(
                    "{} conversations disappeared from upstream!",
                    len(existing_conversations),
                )
            # Nothing at all has happened yet.
            return
        # Fetch more conversations until the oldest one we've fetched
        # is strictly older than the most recent one we already had.
        while existing_conversations and min():
            pass

    def _handle_message(self, mtype, data):
        if mtype == "addAccount":
            raise ClientError("addAccount not yet implemented")
        if mtype == "removeAccount":
            raise ClientError("removeAccount not yet implemented")
        if mtype == "getAccounts":
            return {
                "messenger": {
                    "service": "messenger",
                    "name": "Messenger",
                    "loginRequired": not self.logged_in,
                    "loginFields": self.service.get_login_fields(),
                }
            }
        if mtype == "login":
            self._get_aid(data)
            try:
                self.service.logout()
            except (ClientError, LoginRequiredError):
                pass
            fields = data.get("fields")
            if not isinstance(fields, dict):
                raise ClientError("login fields missing or not a map")
            for key, value in fields.items():
                if not (isinstance(key, str) and isinstance(value, str)):
                    raise ClientError("login fields include non-strings")
            if set(fields) != set(f["field"] for f in self.service.get_login_fields()):
                raise ClientError("login fields do not match required field names")
            self.service.login(fields)
            self.logged_in = True
            store.set_session("messenger", self.service.get_session())
            return {}
        if mtype == "logout":
            self._get_aid()
            self.service.logout()
            self.logged_in = False
            store.set_session("messenger", None)
            return {}
        if mtype == "getConversations":
            self._get_aid(data)
            account_data = store.get_account_data("messenger")
            if account_data is None:
                account_data = {"name": "Messenger", "users": {}, "conversations": []}
            existing_cids = {
                c["id"]: idx for idx, c in enumerate(account_data["conversations"])
            }
            you = self.service.get_you()
            users_with_data_needed = set()
            users_with_data_fetched = set()
            service_data = self.service.get_conversations(before=None)
            if account_data["conversations"] and not service_data["conversations"]:
                raise ServiceError("upstream forgot about all our conversations")
            elif account_data["conversations"] and service_data["conversations"]:
                while (
                    service_data["conversations"][-1]["timestamp"]
                    >= account_data["conversations"][0]["timestamp"]
                ):
                    older_service_data = self.service.get_conversations(
                        before=service_data["conversations"][-1]["timestamp"]
                    )
                    if not older_service_data["conversations"]:
                        break
                    for conversation in older_service_data:
                        if conversation["id"] not in existing_cids:
                            service_data["conversations"].append(conversation)
                    for uid, user in older_service_data.get("users", {}).items():
                        if "users" not in service_data:
                            service_data["users"] = {}
                        if uid not in service_data["users"]:
                            service_data["users"][uid] = {}
                        name = user.get("name")
                        if name:
                            service_data["users"][uid]["name"] = name
                            users_with_data_fetched.add(uid)
            if len(set(c["id"] for c in service_data["conversations"])) != len(
                service_data["conversations"]
            ):
                raise ServiceError("upstream returned non-unique conversation IDs")
            if not util.is_sorted(
                service_data["conversations"], key=lambda c: -c["timestamp"]
            ):
                raise ServiceError(
                    "upstream returned conversations out of timestamp order"
                )
            for conversation in service_data["conversations"]:
                assert not conversation.get(
                    "messages"
                ), "can't handle eager message fetch yet"
                cid = conversation["id"]
                if cid in existing_cids:
                    existing_conversation = account_data["conversations"][
                        existing_cids[cid]
                    ]
                    existing_conversation["name"] = conversation["name"]
                    existing_conversation["timestamp"] = conversation["timestamp"]
                    for uid in list(existing_conversation["participants"]):
                        if uid not in conversation["participants"]:
                            existing_conversation["participants"].pop(uid)
                        if uid not in existing_conversation["participants"]:
                            existing_conversation["participants"][uid] = {}
                        user = conversation["participants"][uid]
                        last_seen_message = user.get("lastSeenMessage")
                        if last_seen_message:
                            existing_conversation["participants"][uid][
                                "lastSeenMessage"
                            ] = last_seen_message
                    existing_conversation["participants"] = {
                        uid: {
                            "lastSeenMessage": participant.get("lastSeenMessage")
                            or existing_conversation["participants"].get(uid, {})[
                                "lastSeenMessage"
                            ]
                        }
                        for uid, participant in conversation.get(
                            "participants", {}
                        ).items()
                    }
                else:
                    account_data["conversations"].append(
                        {
                            "id": conversation["id"],
                            "name": conversation["name"],
                            "timestamp": conversation["timestamp"],
                            "participants": {
                                uid: {
                                    "lastSeenMessage": participant.get(
                                        "lastSeenMessage"
                                    )
                                }
                                for uid, participant in conversation[
                                    "participants"
                                ].items()
                            },
                            "messages": [],
                        }
                    )
                users_with_data_needed.update(conversation["participants"])
            account_data["conversations"].sort(key=lambda c: -c["timestamp"])
            # TODO: Fetch the older conversations here.
            extra_user_info = self.service.get_users(
                users_with_data_needed - users_with_data_fetched
            )
            for uid, user in extra_user_info.items():
                if "users" not in service_data:
                    service_data["users"] = {}
                service_data["users"][uid] = {"name": user["name"]}
            for uid, user in service_data["users"].items():
                if "users" not in account_data:
                    account_data["users"] = {}
                account_data["users"][uid] = {"name": user["name"]}
            result = {
                "conversations": [
                    {
                        "id": c["id"],
                        "name": c["name"],
                        "timestamp": c["timestamp"],
                        "participants": [
                            {
                                "id": uid,
                                "name": account_data["users"][uid]["name"],
                                "you": uid == you,
                            }
                            for uid, p in c["participants"].items()
                        ],
                    }
                    for c in account_data["conversations"]
                ]
            }
            store.set_account_data("messenger", account_data)
            return result
        if mtype == "getMessages":
            raise ClientError("getMessages not yet implemented")
        if mtype == "sendMessage":
            raise ClientError("sendMessage not yet implemented")
        raise ClientError("unknown message type: {}".format(mtype))

    def handle_message(self, client_msg):
        try:
            if not isinstance(client_msg, dict):
                raise ClientError("message not a map")
            mid = client_msg.get("id")
            mtype = client_msg.get("type")
            data = client_msg.get("data")
            if not isinstance(mid, str):
                raise ClientError("message ID missing or not a string")
            if not isinstance(mtype, str):
                raise ClientError("message type missing or not a string")
            if not isinstance(data, dict):
                raise ClientError("message data missing or not a map")
            data = self._handle_message(mtype, data)
            self.send_msg({"type": "response", "id": mid, "error": None, "data": data})
        except ClientError as e:
            self.send_msg(
                {"type": "response", "id": mid, "error": "client error: {}".format(e)}
            )
        except ServiceError as e:
            self.send_msg(
                {
                    "type": "response",
                    "id": mid,
                    "error": "unexpected error: {}".format(e),
                }
            )
        except LoginRequiredError:
            self._ask_for_login()
            self.send_msg({"type": "response", "id": mid, "error": "login required"})
