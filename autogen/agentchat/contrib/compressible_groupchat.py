from typing import Callable, Dict, Optional, Union, Tuple, List, Any
from autogen import OpenAIWrapper
from autogen import Agent, ConversableAgent
import sys
from autogen.token_count_utils import count_token, get_max_token_limit, num_tokens_from_functions

try:
    from termcolor import colored
except ImportError:

    def colored(x, *args, **kwargs):
        return x


from autogen.agentchat.contrib.compressible_agent import CompressibleAgent
from autogen.agentchat.groupchat import GroupChat


class CompressibleGroupChatManager(CompressibleAgent):
    """(In preview) A chat manager agent that can manage a group chat of multiple agents."""

    def run_chat(
        self,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Agent] = None,
        config: Optional[GroupChat] = None,
    ) -> Union[str, Dict, None]:
        """Run a group chat."""
        if messages is None:
            messages = self._oai_messages[sender]
        message = messages[-1]
        speaker = sender
        groupchat = config
        for i in range(groupchat.max_round):
            # set the name to speaker's name if the role is not function
            if message["role"] != "function":
                message["name"] = speaker.name

            # check if the groupchat is over the limit, and compress if needed
            is_terminte = self.on_groupchat_limit(groupchat, message, speaker)
            if is_terminte:
                break

            groupchat.messages.append(message)
            # broadcast the message to all agents except the speaker
            for agent in groupchat.agents:
                if agent != speaker:
                    self.send(message, agent, request_reply=False, silent=True)
            if i == groupchat.max_round - 1:
                # the last round
                break
            try:
                # select the next speaker
                speaker = groupchat.select_speaker(speaker, self)
                # let the speaker speak
                reply = speaker.generate_reply(sender=self)
            except KeyboardInterrupt:
                # let the admin agent speak if interrupted
                if groupchat.admin_name in groupchat.agent_names:
                    # admin agent is one of the participants
                    speaker = groupchat.agent_by_name(groupchat.admin_name)
                    reply = speaker.generate_reply(sender=self)
                else:
                    # admin agent is not found in the participants
                    raise
            if reply is None:
                break
            # The speaker sends the message without requesting a reply
            speaker.send(reply, self, request_reply=False)
            message = self.last_message(speaker)
        return True, None

    def on_groupchat_limit(self, groupchat: GroupChat, new_message: Dict, last_speaker: Agent):
        # disabled if compress_config is False
        if self.compress_config is False:
            return False

        # we will only count the token used by groupmanager
        token_used = count_token(
            groupchat.select_speaker_msg + groupchat.messages + [new_message] + groupchat.selector_end_msg,
            self.llm_config["model"],
        )
        max_token = max(get_max_token_limit(self.llm_config["model"]), self.llm_config.get("max_token", 0))

        final, compressed_messages = self._manage_history_on_token_limit(
            groupchat.messages, token_used, max_token, last_speaker
        )
        # False, None -> no compression is needed
        # False, compressed_messages -> compress success
        # True, None -> terminate
        if final:
            return True

        if compressed_messages is not None:
            groupchat.messages = compressed_messages
            # update all agents' messages
            for agent in groupchat.agents:
                if agent != last_speaker:
                    agent._oai_messages[self] = self._convert_agent_messages(compressed_messages, agent)
                else:
                    agent._oai_messages[self] = self._convert_agent_messages(compressed_messages, agent) + [
                        agent.last_message(self)
                    ]
            return False  # don't terminate

        elif max_token - token_used <= 0:
            print(
                f"Warning: Compression failed, but no token left, terminate. (max_token allowed for {self.llm_config['model']}: {max_token}, current token count: {token_used})"
            )
            return True
        else:
            print(
                f"Warning: Compression failed, but didn't reached max_token, continue. (max_token allowed for {self.llm_config['model']}: {max_token}, current token count: {token_used})"
            )
            return False

    def _convert_agent_messages(self, compressed_messages, agent):
        converted_messages = []

        for cmsg in compressed_messages.copy():
            if cmsg["role"] == "function" or cmsg["role"] == "system":
                pass  # do nothing
            elif cmsg["name"] == agent.name:
                del cmsg["name"]
                cmsg["role"] = "assistant"
            elif "function_call" in cmsg:
                cmsg["role"] = "assistant"  # only messages with role 'assistant' can have a function call.
            else:
                cmsg["role"] = "user"
            converted_messages.append(cmsg)
        return converted_messages
