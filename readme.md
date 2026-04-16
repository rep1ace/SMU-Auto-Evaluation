# 南方医科大学自动评课脚本
如题，实现南医教务课程自动评价。支持小范围的随机打分。
# 使用方法
# 环境配置
## 1. 安装uv
windows终端运行
```
powershell -c "irm https://astral.sh/uv/install.ps1 | more"
```
或
```
pip install uv
```
执行完毕后，关闭终端窗口然后重新打开
## 2. 配置uv环境
1. 下载解压项目文件： 项目主页 - 绿色Code按钮 - Download Zip 
2. 打开解压后的文件夹，在空白处单击右键-在终端中打开，然后运行
```
uv sync
```
项目已内置验证码识别模型。请保留 `models/captcha_model.onnx` 与 `models/captcha_model.onnx.data` 这两个文件，它们需要同时存在。
## 3. 更改config.ini

 配置文件格式：
```
 [login]
account=你的账号
password=你的密码
```
**至此环境配置已经完成，你可以在文件夹目录中终端运行`uv run main.py`测试程序是否正常运行**
# 定时任务设置
Win+R 运行

     taskschd.msc
### 点击 操作 - 创建任务
触发器选项卡:
 * 按预定计划-每天 时间最好选早一些比如00:01

操作选项卡:
* 程序或脚本：选择 下载目录\SMU-Auto-Evaluation-Master\.venv\Scripts\python.exe
* 添加参数: main.py
* 起始于: 下载目录\SMU-Auto-Evaluation-Master（脚本所在目录）

设置选项卡：
* **勾选[如果过了计划开始时间，立即启动任务]**

 # Enjoy
