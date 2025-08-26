import wxauto
from wxauto import WeChat

wx = WeChat()

#wx.SendMsg("hi","文件传输助手")

msgs = wx.GetAllMessage()
for msg in msgs:
    print(f'消息内容:{msg.comtent},消息类型:{msg.type}')