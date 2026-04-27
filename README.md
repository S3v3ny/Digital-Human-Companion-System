# Digtal-Human-Companion-System

【A22】基于AI大语言模型的情感陪护虚拟数字人系统

---

## 项目介绍

---

## 快速部署

### 1. 克隆仓库

转到下载文件夹

```bash
cd 目标文件夹目录
```

克隆仓库

```bash
git clone https://github.com/CtrlCSV/Digital-Human-Companion-System
```

### 2. 文件补全

本项目使用了[thu-coai/PsyQA](https://github.com/thu-coai/PsyQA "一个中文心理健康支持问答数据集，提供了丰富的援助策略标注。可用于生成富有援助策略的长咨询文本。")数据集，可跳转到对应仓库下载（下载后直接放到项目文件夹下即可），也可使用其他数据集。

其余模型文件提供网盘下载链接，放到对应目录下即可。

```plaintext
通过网盘分享的文件：Digtal-Human-Companion-System
链接: https://pan.baidu.com/s/1lni_D5WtU01ISW8zi4oQWQ?pwd=66mg 提取码: 66mg 
--来自百度网盘超级会员v1的分享
```

### 3. 安装环境

#### conda安装

```bash
#根据依赖创建环境
conda env create -f environment.yml --name 你想要的环境名字

#激活环境
conda activate 你想要的环境名字
```

也可自行使用uv或pip安装依赖，提供了 `requirement.txt`供使用

### 4. 数据库加载

直接运行 `build_kb.py`即可生成数据库文件，第一次使用会在线下载 `BGE-small-zh-v1.5`模型。

```bash
python build_kb.py
```

### 5. 启动项目

```bash
python server.py
```



---



## 镜像打包

### 1. 启动docker（以windows系统为例）

双击运行Docker Desktop

### 2. 打包镜像

```bash
cd 项目目录
docker compose build
```


### 我们也提供了打包好的镜像以供下载：

```plaintext
通过网盘分享的文件：digital-human.tar.gz
链接: https://pan.baidu.com/s/1O3mpjuZEwIx1IbqzgB5stg?pwd=eyqz 提取码: eyqz 
--来自百度网盘超级会员v1的分享
```
