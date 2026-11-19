# PhotoCleaner

本地运行的中文图像整理工具。支持按像素、文件体积和内容筛图，目录整理、哈希去重、兼容修复、压缩，以及文字/参考图搜索。

## 开始使用

本机双击 start.bat 启动。新电脑准备 Python 3.12 后运行 setup.bat 安装依赖，再按页面提示准备模型。模型完整缓存后优先本地运行。

添加输入目录，先预览明细，再执行。子目录自动参与。各功能独立保存条件和进度，输出可按需要保留来源层级。运行期间请让相关目录保持原样。

## 常用功能

| 功能 | 操作结果 |
| --- | --- |
| 像素尺寸 / 文件体积筛选 | 按宽高或字节范围剪切入选图像 |
| 表情包 / 实拍 / 人物与活动 / 绘画与设计 | 结合各页内容规则选择图像，再按门槛剪切 |
| 扁平整理 | 三种互斥方式，决定主文件和多余副本的位置 |
| 哈希原位去重 | 按目录优先级保留原位文件，重复副本进入回收站 |
| 哈希归集 / 基准去重 | 集中保留主文件，或对照固定基准清理副本 |
| 命名分组 | 在各原目录内按命名规律归类，支持追加后再分组 |
| 图像兼容修复 | 检查 Windows 解码与后缀，生成兼容副本并保留原件 |
| 文字 / 以图搜图 | 共用索引，预览勾选后复制候选 |
| 大图压缩 | 生成原尺寸 JPG 副本，按条件对比和回写 |

每页的“使用说明”提供具体步骤；文字来源位于 [help](help/)，默认处理条件位于 [defaults](defaults/)，可直接编辑。

## 复核与执行

明细支持表格与缩略图两种视图，共用状态、分组和排序。按内容筛图的“门槛影响”会比较条件调整前后的新增、移出和保留项，再按需应用条件。已有兼容证据会继续复用。

执行前的“文件去向”分别显示剪切、复制和回收的数量、文件体积与恢复方式。先预览即可查看完整计划。

## 文件与进度

同名不同内容自动编号保存。筛图可把入选重复副本另放“重复副本备份”。原位去重的保留文件及所有文件夹维持原路径，回收站保留被回收文件的原位置。

输出 .photocleaner 与程序 .jobs 保存判定、报告、索引和操作记录。保留这些记录，方便继续任务、复用分数和恢复剪切。“缓存与数据库”可查看实际位置及迁移选项。模型版本、文件变化或必要证据变化时，程序会更新相关判定。

## 命令行例子

默认生成预览，添加 --move 执行对应操作。原位模式按 --input 中目录从左到右优先保留：

    .\.venv\Scripts\python.exe run.py hash --in-place --input "D:\图库A" "E:\图库B"
    .\.venv\Scripts\python.exe run.py hash --in-place --input "D:\图库A" "E:\图库B" --move
    .\.venv\Scripts\python.exe run.py run --input "D:\照片" --output "E:\筛选结果" --offline
    .\.venv\Scripts\python.exe run.py repair --input "D:\待修复" --output "E:\兼容副本" --move

更多参数使用各子命令的 --help。

## 从源码安装

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[ai,dev]"
.\.venv\Scripts\python.exe -m photocleaner
```

Windows 使用 setup.bat 可自动选择本机的 PyTorch 运行环境。基础依赖也可用 `pip install -e .` 单独安装；内容筛选和语义搜索使用 ai 依赖及对应的本地模型。

项目采用平铺 Python 包结构：photocleaner/ 为程序，help/ 和 defaults/ 为运行资源，tests/unit/ 为使用生成数据的核心测试，scripts/ 为发行检查，.github/workflows/ 为自动检查。运行 `python -m pytest` 和 `python scripts/check_release.py` 可检查确定性逻辑与发行范围。

源码运行继续使用程序旁的设置、模型及任务记录；安装包运行时使用用户的本地应用目录。可用 PHOTOCLEANER_HOME 指定其他数据位置。
