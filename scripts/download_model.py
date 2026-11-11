#!/usr/bin/env python3
"""
Script de Download Automatizado do Modelo GGUF Quantizado.
Baixa o modelo Qwen 2.5 1.5B (ou 3B) Instruct Q4_K_M do Hugging Face
com suporte a barra de progresso e fallback sem dependências pesadas.
"""

import argparse
import os
import sys
from pathlib import Path
import urllib.request

DEFAULT_REPO_ID = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
DEFAULT_FILENAME = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
DEFAULT_TARGET_DIR = Path(__file__).resolve().parent.parent / "models"


def download_with_hf_hub(repo_id: str, filename: str, target_dir: Path):
    """Método preferencial via biblioteca huggingface_hub."""
    try:
        from huggingface_hub import hf_hub_download
        print(f"[*] Iniciando download via huggingface_hub:")
        print(f"    Repositório: {repo_id}")
        print(f"    Arquivo:     {filename}")
        print(f"    Destino:     {target_dir}")

        target_dir.mkdir(parents=True, exist_ok=True)
        local_path = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            local_dir=str(target_dir)
        )
        print(f"[+] Download concluído com sucesso em: {local_path}")
        return local_path
    except ImportError:
        print("[!] huggingface_hub não instalado. Utilizando download direto HTTP...")
        return download_with_urllib(repo_id, filename, target_dir)


def download_with_urllib(repo_id: str, filename: str, target_dir: Path):
    """Método de fallback via urllib nativo do Python (zero dependências extras)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / filename

    if destination.exists() and destination.stat().st_size > 100_000_000:
        print(f"[*] Arquivo já existe e parece íntegro ({destination.stat().st_size / (1024*1024):.1f} MB): {destination}")
        return str(destination)

    url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
    print(f"[*] Baixando diretamente de: {url}")
    print(f"    Salvando em: {destination}")

    def reporthook(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            percent = min(100.0, (downloaded / total_size) * 100.0)
            mb_down = downloaded / (1024 * 1024)
            mb_total = total_size / (1024 * 1024)
            sys.stdout.write(f"\r    Progresso: {percent:5.1f}% [{mb_down:6.1f} MB / {mb_total:6.1f} MB]")
            sys.stdout.flush()

    try:
        urllib.request.urlretrieve(url, str(destination), reporthook=reporthook)
        print("\n[+] Download finalizado com sucesso!")
        return str(destination)
    except Exception as e:
        print(f"\n[-] Erro durante o download: {e}")
        if destination.exists():
            destination.unlink()
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Download do modelo GGUF para iaparacasa Edge AI.")
    parser.add_argument("--repo", default=DEFAULT_REPO_ID, help="ID do repositório Hugging Face")
    parser.add_argument("--file", default=DEFAULT_FILENAME, help="Nome do arquivo GGUF")
    parser.add_argument("--dir", default=str(DEFAULT_TARGET_DIR), help="Diretório de destino")
    parser.add_argument("--use-3b", action="store_true", help="Baixa alternativa de maior capacidade: Qwen 2.5 3B Instruct")

    args = parser.parse_args()

    repo = args.repo
    filename = args.file

    if args.use_3b:
        repo = "Qwen/Qwen2.5-3B-Instruct-GGUF"
        filename = "qwen2.5-3b-instruct-q4_k_m.gguf"

    download_with_hf_hub(repo_id=repo, filename=filename, target_dir=Path(args.dir))


if __name__ == "__main__":
    main()

