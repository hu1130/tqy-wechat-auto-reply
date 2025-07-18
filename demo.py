import requests
import json
import re


def func(question, model='deepseek-r1:14b', API_URL="http://localhost:11434/api/generate"):
    headers = {"Content-Type": "application/json"}
    data = {"model": model, "prompt": question, "stream": True}



    state = "before" # before|in|out
    buffer = ""
    start_tag = "<think>"
    end_tag = "</think>"


    response = requests.post(API_URL, headers=headers, json=data, stream=True)

    for line in response.iter_lines():
        if line:
            json_data = json.loads(line.decode("utf-8"))
            #print(json_data)
            char = json_data.get("response", "")

            if state == 'out':
                print(char,end = '',flush = True)
            else:
                buffer += char

            if state == "before":
                # 查找开始标记的位置
                start_idx = buffer.find(start_tag)
                if start_idx != -1:
                    # 输出开始标记之前的内容
                    print(buffer[:start_idx], end="", flush=True)
                    buffer = buffer[start_idx + len(start_tag):]  # 保留开始标记之后的内容
                    state = "in"
                else:
                    # 检查是否需要保留部分字符以防标记被拆分
                    keep_chars = len(start_tag) - 1
                    if len(buffer) > keep_chars:
                        safe_output = buffer[:-keep_chars]
                        buffer = buffer[-keep_chars:]
                        print(safe_output, end="", flush=True)


            if state == "in":
                # 查找结束标记的位置
                end_idx = buffer.find(end_tag)
                if end_idx != -1:
                    buffer = buffer[end_idx + len(end_tag):]  # 删除结束标记及其之前的内容
                    state = "out"
                    if buffer:
                        print(buffer, end="", flush=True)
                else:
                    # 检查结束标记是否可能被拆分
                    max_keep = len(end_tag) - 1
                    keep_prefix = 0
                    for i in range(1, min(max_keep, len(buffer)) + 1):
                        if buffer[-i:] == end_tag[:i]:
                            keep_prefix = i

                    if len(buffer) > max_keep:
                        # 删除非结尾的部分（因为它们在标记中）
                        buffer = buffer[-keep_prefix:] if keep_prefix > 0 else ""

                # 处理残留的缓冲区内容
            if state in ["before", "out"]:
                print(buffer, end="", flush=True)
            elif state == "IN_TAG":
                # 不输出未结束的标记内容
                pass

func('hello', model='deepseek-r1:14b')