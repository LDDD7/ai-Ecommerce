"""项目自带的运维/评测脚本。

这里必须有 ``__init__.py``：环境里曾装过一个同名 ``tools`` 包
（``D:\Python\lib\site-packages\tools``），正则包优先于命名空间包，
于是 ``import tools.vision_eval`` 会去找那个包并报 ModuleNotFoundError。
把本目录变成正规包后，当前目录（sys.path 第一位）的 ``tools`` 会胜出。
"""
