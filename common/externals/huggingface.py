import asyncio
import base64
import io
import random
import re
from typing import List, Optional

import aiohttp
import websockets

from common import json
from common.externals.animegan import mask_anime2  # noqa: F401 (public re-export)
from common.externals.exceptions import BadRequestError
from common.utils import retry_async_, bytes_io_to_base64, base64_to_bytes_io


@retry_async_(retries_count=1, sleep_for=5.)
async def hg_base(repo: str, data: dict) -> dict:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        'referrer': f'https://hf.space/gradioiframe/{repo}/+',
    }

    async with aiohttp.ClientSession() as session:
        url = f'https://hf.space/gradioiframe/{repo}/api/queue/push/'
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

        task_id = result['hash']
        data = {'hash': task_id}
        url = f'https://hf.space/gradioiframe/{repo}/api/queue/status/'
        await asyncio.sleep(5.)

        while True:
            async with session.post(url, headers=headers, json=data) as response:
                if response.status != 200:
                    print(await response.text())
                    raise BadRequestError()
                result = json.loads(await response.read())
                if result['status'] not in ('QUEUED', 'PENDING'):
                    if result['status'] == 'COMPLETE':
                        break
                    else:
                        print(await response.text())
                        raise BadRequestError()

                await asyncio.sleep(5.)

    return result['data']


async def hg(file: io.BytesIO, repo: str, additional_data: List[str] = None) -> io.BytesIO:
    additional_data = additional_data or []
    data = {
        'action': 'predict',
        'data': [bytes_io_to_base64(file)] + additional_data,
    }
    result = await hg_base(repo, data)
    return base64_to_bytes_io(result['data'][0])


async def mask_drag(file: io.BytesIO) -> io.BytesIO:
    return await hg(file, 'Norod78/Dragness')


async def mask_doll(file: io.BytesIO) -> io.BytesIO:
    return await hg(file, 'Norod78/Face2Doll')


async def mask_vintage(file: io.BytesIO) -> io.BytesIO:
    return await hg(file, 'Norod78/VintageStyle')


async def mask_arcane(file: io.BytesIO) -> io.BytesIO:
    data = {
        'action': 'predict',
        'data': [bytes_io_to_base64(file), 'version 0.4'],
        'fn_index': 0,
        'session_hash': 'x3vuogz1mua',
    }
    result = await hg_base('akhaliq/ArcaneGAN', data)
    return base64_to_bytes_io(result['data'][0])


async def mask_arcane_video(file: io.BytesIO, duration: int, mime_type: str = 'video/mp4') -> io.BytesIO:
    data = {
        'action': 'predict',
        'data': [
            {'data': bytes_io_to_base64(file, mime_type), 'is_example': False, 'name': 'file.mp4'},
            0, duration, 18, 'No'
        ],
    }
    result = await hg_base('sxela/ArcaneGAN-video', data)
    return base64_to_bytes_io(result['data'][0]['data'])


async def mask_paint(file: io.BytesIO) -> io.BytesIO:
    data = {
        'action': 'predict',
        'data': [bytes_io_to_base64(file)],
        'example_id': None,
        'session_hash': '2c0siv1p4lu',
        'cleared': False,
    }
    result = await hg_base('akhaliq/PaintTransformer', data)
    return base64_to_bytes_io(result['data'][0])


async def mask_inpaint(file: io.BytesIO) -> io.BytesIO:
    image = bytes_io_to_base64(file)
    data = {
        'action': 'predict',
        'data': [{'image': image, 'mask': image}, 'automatic (U2net)'],
        'fn_index': 0,
        'session_hash': '1hrw298g3eni',
    }
    result = await hg_base('akhaliq/lama', data)
    return base64_to_bytes_io(result['data'][0])


async def mask_inter(file_1: io.BytesIO, file_2: io.BytesIO) -> io.BytesIO:
    data = {
        'action': 'predict',
        'data': [bytes_io_to_base64(file_1), bytes_io_to_base64(file_2), 4],
        'example_id': None,
        'session_hash': 'ph414mkioq',
        'cleared': False,
    }
    result = await hg_base('akhaliq/frame-interpolation', data)
    return base64_to_bytes_io(result['data'][0]['data'])


async def hg_latent_diffusion(text: str) -> io.BytesIO:
    data = {
        'action': 'predict',
        'data': [text, 50, 256, 256, 4, 5],
        'example_id': None,
        'session_hash': 'luinwvm5sdf',
        'cleared': False,
    }
    result = await hg_base('multimodalart/latentdiffusion', data)
    return base64_to_bytes_io(result['data'][0])


async def hg_copilot(source: str) -> str:
    splits = source.split('<infill>')

    params = {
        'info': base64.urlsafe_b64encode(bytes(json.dumps({
            'length': '80',
            'temperature': '0.4',
            'extra_sentinel': False,
            'parts': splits,
            'prompt': source,
        }), 'utf-8')).decode('utf-8'),
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        'referrer': 'https://hf.space/embed/facebook/incoder-demo/+',
    }

    async with aiohttp.ClientSession() as session:
        url = f'https://hf.space/embed/facebook/incoder-demo/{"infill" if len(splits) > 1 else "generate"}'
        async with session.get(url, headers=headers, params=params) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

    def clean_code(code: str) -> str:
        return re.sub(r'<\|[^\n]+\|>', '', code).strip()

    return clean_code(result['text'])


async def mask_yolo(file: io.BytesIO) -> io.BytesIO:
    return await hg(file, 'pytorch/YOLOv5')


async def mask_privacy(file: io.BytesIO) -> io.BytesIO:
    data = {
        'data': [bytes_io_to_base64(file), random.uniform(0., 4.)],
        'cleared': False,
        'example_id': None,
        'session_hash': '1p2s49c2271',
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        'referrer': 'https://hf.space/gradioiframe/haakohu/DeepPrivacy/+',
    }

    async with aiohttp.ClientSession() as session:
        url = f'https://hf.space/gradioiframe/haakohu/DeepPrivacy/api/predict/'
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

    return base64_to_bytes_io(result['data'][0])


async def hg_dalle(text: str) -> List[io.BytesIO]:
    data = {
        'prompt': text,
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:102.0) Gecko/20100101 Firefox/102.0",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.5",
        "referrer": "https://hf.space/",
    }

    async with aiohttp.ClientSession() as session:
        url = f'https://bf.dallemini.ai/generate'
        async with session.post(url, headers=headers, json=data) as response:
            if response.status != 200:
                raise BadRequestError()
            result = json.loads(await response.read())

    return [base64_to_bytes_io(s) for s in result['images'][:4]]


async def hg_stable_diffusion(text: str) -> Optional[List[io.BytesIO]]:
    url = 'wss://runwayml-stable-diffusion-v1-5.hf.space/queue/join'

    async with websockets.connect(url) as websocket:
        async for message in websocket:
            data = json.loads(message)

            if data['msg'] == 'send_data':
                send_data = json.dumps({
                    "fn_index": 2,
                    "data": [text],
                    "session_hash": "z4bulqm1jac"
                })
                await websocket.send(send_data)

            elif data['msg'] == 'send_hash':
                send_data = json.dumps({
                    "fn_index": 2,
                    "session_hash": "z4bulqm1jac"
                })
                await websocket.send(send_data)

            elif data['msg'] == 'process_completed':
                if 'data' not in data['output']:
                    raise BadRequestError()
                return [base64_to_bytes_io(s) for s in data['output']['data'][0]]
