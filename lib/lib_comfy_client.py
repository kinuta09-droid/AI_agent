# -*- coding: utf-8 -*-
"""
ComfyUI API Client
ComfyUI (localhost:8188) との通信を担当する汎用クライアント。
WD14 Tagger、img2img 等、あらゆるワークフローの API 呼び出しに使用。
"""

import os
import sys
import time
import json
import requests

sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv

from lib.paths import ENV_PATH

load_dotenv(ENV_PATH)

COMFYUI_HOST = os.getenv("COMFYUI_HOST", "127.0.0.1")
COMFYUI_PORT = int(os.getenv("COMFYUI_PORT", "8188"))


class ComfyUIClient:
    """ComfyUI REST API クライアント"""

    def __init__(self, host: str = None, port: int = None):
        host = host or COMFYUI_HOST
        port = port or COMFYUI_PORT
        self.base_url = f"http://{host}:{port}"

    def is_alive(self) -> bool:
        """ComfyUI サーバーが起動しているか確認する。"""
        try:
            r = requests.get(f"{self.base_url}/system_stats", timeout=5)
            return r.status_code == 200
        except requests.exceptions.ConnectionError:
            return False
        except Exception:
            return False

    def upload_image(self, image_path: str, subfolder: str = "input",
                     overwrite: bool = True) -> str:
        """画像を ComfyUI の input フォルダにアップロードする。

        Returns:
            アップロード後のファイル名（ComfyUI 側での名前）
        """
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"画像が見つかりません: {image_path}")

        filename = os.path.basename(image_path)
        with open(image_path, 'rb') as f:
            files = {"image": (filename, f, "image/png")}
            data = {
                "subfolder": subfolder,
                "overwrite": str(overwrite).lower(),
            }
            r = requests.post(
                f"{self.base_url}/upload/image",
                files=files,
                data=data,
                timeout=30,
            )
            r.raise_for_status()

        result = r.json()
        uploaded_name = result.get("name", filename)
        sub = result.get("subfolder", "")
        if sub:
            return f"{sub}/{uploaded_name}"
        return uploaded_name

    def queue_prompt(self, workflow: dict) -> str:
        """ワークフローをキューに投入し、prompt_id を返す。

        Args:
            workflow: ComfyUI API 形式のワークフロー JSON (ノードID → ノード定義)
        """
        payload = {"prompt": workflow}
        r = requests.post(
            f"{self.base_url}/prompt",
            json=payload,
            timeout=30,
        )
        r.raise_for_status()

        result = r.json()
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"prompt_id が返されませんでした: {result}")
        return prompt_id

    def get_history(self, prompt_id: str) -> dict:
        """指定した prompt_id の実行履歴を取得する。"""
        r = requests.get(
            f"{self.base_url}/history/{prompt_id}",
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def wait_for_completion(self, prompt_id: str,
                            timeout: int = 120,
                            poll: float = 1.0) -> dict:
        """プロンプトの実行完了をポーリングで待機する。

        Returns:
            完了時の history データ（outputs 含む）

        Raises:
            TimeoutError: 指定時間内に完了しなかった場合
        """
        start = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                raise TimeoutError(
                    f"ComfyUI の処理が {timeout}秒 以内に完了しませんでした "
                    f"(prompt_id: {prompt_id})"
                )

            history = self.get_history(prompt_id)
            if prompt_id in history:
                entry = history[prompt_id]
                status = entry.get("status", {})
                if status.get("completed", False):
                    return entry
                if status.get("status_str") == "error":
                    msgs = entry.get("status", {}).get("messages", [])
                    raise RuntimeError(
                        f"ComfyUI 実行エラー: {msgs}"
                    )

            time.sleep(poll)
