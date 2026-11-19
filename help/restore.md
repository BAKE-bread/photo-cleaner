# 恢复剪切与分组

软件中的“恢复本次剪切”或“恢复上次分组”会按记录把本次移动的文件移回原路径。保留输出内的 .photocleaner 记录，便于核对与恢复。

原位置已有文件、目标内容发生变化时，该项会保留现状并说明原因，其他可恢复项继续处理。哈希去重回收的文件可使用 Windows 回收站恢复；原位去重的回收站记录保留原来源路径。

命令行用法
恢复实拍页的已记录剪切：
python -m photocleaner restore --output "D:\图像输出" --task photo

指定某次运行：
python -m photocleaner restore --output "D:\图像输出" --task photo --run-id 运行ID

--task 可选择 photo、people、creative、meme、flatten、small、file_size 或 group。命名分组使用 --input 提供原输入目录。内容筛图的记录位于 .photocleaner 下各自功能目录。

进阶说明
恢复会核对目标哈希与原位置。旧共享记录按 config.scheme 匹配所属功能；明确指定 --run-id 可定位旧运行。恢复期间相关任务目录由程序独占，完成后可查看报告。
