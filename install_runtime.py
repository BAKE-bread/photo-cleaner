"""Install the torch build that matches the detected Windows NVIDIA driver."""
import argparse
from pathlib import Path
import re
import subprocess
import sys


def cuda_driver():
    try:
        result=subprocess.run(['nvidia-smi'],capture_output=True,text=True,timeout=15)
        match=re.search(r'CUDA Version:\s*(\d+)\.(\d+)',result.stdout)
        return tuple(map(int,match.groups())) if match else None
    except (OSError,subprocess.TimeoutExpired):
        return None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--cuda',action='store_true')
    parser.add_argument('--cpu',action='store_true')
    parser.add_argument('--torch-only',action='store_true')
    args=parser.parse_args()
    if args.cuda and args.cpu:parser.error('Choose only one runtime.')
    driver=cuda_driver()
    cuda=not args.cpu and bool(driver and driver>=(12,6))
    if args.cuda and not cuda:
        raise SystemExit('CUDA 12.6 or later driver is required for this build. Update the NVIDIA driver, or use CPU.')
    build='cu126' if cuda else 'cpu'
    print(f'Installing PyTorch {build}; driver capability: {driver}',flush=True)
    subprocess.check_call([sys.executable,'-m','pip','install','--upgrade',f'torch==2.14.1+{build}',
                           '--index-url',f'https://download.pytorch.org/whl/{build}'])
    if not args.torch_only:
        subprocess.check_call([sys.executable,'-m','pip','install','-r',str(Path(__file__).parent/'requirements.txt')])
    subprocess.check_call([sys.executable,'-c','import torch; print("torch:",torch.__version__,"CUDA:",torch.cuda.is_available())'])

if __name__=='__main__':main()
