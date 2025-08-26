import math
cnt = 0
for i in range(1 , 5):
    for x in range(1 , 5):
        for y in range (1 , 5):
            if i == x:
                continue
            if y == x:
                continue
            if i == y:
                continue
            print(i,x,y)
            cnt+=1
print("总共有 %d 个数字"%(cnt))