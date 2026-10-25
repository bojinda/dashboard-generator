"""Synthetic Comfy/CUDA boundary tests; never import PyTorch or operate a GPU."""
import contextlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
GPU0 = 'GPU-2466a5de-d7d9-8799-a5f0-e2fc9fd5d559'
GPU1 = 'GPU-d786e691-b17c-9ebc-5a60-101433ca2adf'


class Device:
    def __init__(self, name):
        self.name, self.type = name, name.split(':')[0]

    def __eq__(self, other):
        return isinstance(other, Device) and self.name == other.name


class Tensor:
    def __init__(self, device='cpu'):
        self.device = Device(device)


class GPU1ConditioningTests(unittest.TestCase):
    def setUp(self):
        self.current, self.events = 0, []
        torch, comfy, mm, sd, folders = [ModuleType(n) for n in
            ('torch', 'comfy', 'comfy.model_management', 'comfy.sd', 'folder_paths')]

        @contextlib.contextmanager
        def device(index):
            previous, self.current = self.current, index
            try:
                yield
            finally:
                self.current = previous

        self.cuda = SimpleNamespace(
            is_available=Mock(return_value=True), device_count=Mock(return_value=2),
            current_device=lambda: self.current, device=device,
            get_device_properties=Mock(side_effect=lambda i: SimpleNamespace(uuid=(GPU0, GPU1)[i])))
        torch.cuda, torch.device, torch.is_tensor = self.cuda, Device, lambda x: isinstance(x, Tensor)
        mm.intermediate_device = Mock(return_value=Device('cpu'))
        mm.free_memory = Mock(side_effect=lambda amount, dev: self.events.append(('offload', dev.name, self.current)))
        mm.soft_empty_cache = Mock(side_effect=lambda **kw: self.events.append(('cache', self.current)))
        self.clip = SimpleNamespace(patcher=SimpleNamespace(load_device=Device('cuda:1'), offload_device=Device('cpu')),
                                    tokenize=Mock(return_value='synthetic tokens'), clone=Mock())
        self.conditioning = [[Tensor(), {'attention_mask': Tensor()}]]

        def encode(*args, **kwargs):
            self.assertEqual(self.current, 1)
            self.events.append(('encode', self.current))
            return self.conditioning

        self.clip.encode_from_tokens_scheduled = Mock(side_effect=encode)
        self.clip.encode_from_tokens = Mock(side_effect=encode)
        sd.load_clip = Mock(return_value=self.clip)
        sd.CLIPType = SimpleNamespace(QWEN_IMAGE='qwen_image')
        folders.get_full_path_or_raise = Mock(return_value='/synthetic/encoder.safetensors')
        folders.get_folder_paths = Mock(return_value=['/synthetic/embeddings'])
        comfy.sd, comfy.model_management = sd, mm
        self.mm, self.sd = mm, sd
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {'torch': torch, 'comfy': comfy, 'comfy.model_management': mm,
                                'comfy.sd': sd, 'folder_paths': folders}).start()
        spec = importlib.util.spec_from_file_location('synthetic_gpu1_node',
                    ROOT / 'comfy_extensions/aihub_qwen_gpu1/__init__.py')
        self.node = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.node)

    def load(self):
        return self.node.AIHubQwenGPU1CLIPLoader().load_clip(
            'qwen_2.5_vl_7b_fp8_scaled.safetensors', GPU0, GPU1)[0]

    def test_loader_keeps_cpu_initialization_and_explicit_gpu1_execution(self):
        clip = self.load()
        options = self.sd.load_clip.call_args.kwargs['model_options']
        self.assertEqual({k: v.name for k, v in options.items()},
                         {'load_device': 'cuda:1', 'offload_device': 'cpu', 'initial_device': 'cpu'})
        self.assertEqual(self.sd.load_clip.call_args.kwargs['clip_type'], 'qwen_image')
        self.assertEqual(self.events, [])  # No encoding at workflow load time.
        self.assertEqual(clip.tokenize('synthetic'), 'synthetic tokens')

    def test_each_conditioning_call_offloads_gpu1_and_restores_default_gpu0(self):
        clip = self.load()
        for method in ('encode_from_tokens_scheduled', 'encode_from_tokens'):
            self.events.clear()
            self.assertIs(getattr(clip, method)('synthetic tokens'), self.conditioning)
            self.assertEqual(self.events, [('encode', 1), ('offload', 'cuda:1', 1), ('cache', 1)])
            self.assertEqual(self.current, 0)
        self.assertEqual(self.mm.free_memory.call_count, 2)

    def test_encoding_failure_still_offloads_and_restores_gpu0(self):
        clip = self.load()
        self.clip.encode_from_tokens_scheduled.side_effect = RuntimeError('synthetic encoder failure')
        with self.assertRaisesRegex(RuntimeError, 'synthetic encoder failure'):
            clip.encode_from_tokens_scheduled('tokens')
        self.assertEqual(self.events, [('offload', 'cuda:1', 1), ('cache', 1)])
        self.assertEqual(self.current, 0)

    def test_cleanup_failure_propagates_instead_of_claiming_success(self):
        clip = self.load()
        self.mm.free_memory.side_effect = RuntimeError('synthetic cleanup failure')
        with self.assertRaisesRegex(RuntimeError, 'synthetic cleanup failure'):
            clip.encode_from_tokens_scheduled('tokens')
        self.assertEqual(self.current, 0)

    def test_gpu_resident_conditioning_is_rejected_before_gpu1_handoff(self):
        clip = self.load()
        self.clip.encode_from_tokens_scheduled.side_effect = None
        self.clip.encode_from_tokens_scheduled.return_value = [[Tensor('cuda:1'), {}]]
        with self.assertRaisesRegex(RuntimeError, 'conditioning_not_on_cpu'):
            clip.encode_from_tokens_scheduled('tokens')
        self.mm.free_memory.assert_called_once()
        self.assertEqual(self.current, 0)

    def test_wrong_gpu_order_or_missing_second_gpu_never_loads_encoder(self):
        self.cuda.get_device_properties.side_effect = lambda i: SimpleNamespace(uuid=(GPU1, GPU0)[i])
        with self.assertRaisesRegex(RuntimeError, 'mapping_mismatch'):
            self.load()
        self.cuda.device_count.return_value = 1
        with self.assertRaisesRegex(RuntimeError, 'requires_two_gpus'):
            self.load()
        self.sd.load_clip.assert_not_called()

    def test_wrong_default_or_gpu_intermediates_are_rejected(self):
        self.current = 1
        with self.assertRaisesRegex(RuntimeError, 'requires_two_gpus'):
            self.load()
        self.current = 0
        self.mm.intermediate_device.return_value = Device('cuda:0')
        with self.assertRaisesRegex(RuntimeError, 'requires_two_gpus'):
            self.load()
        self.sd.load_clip.assert_not_called()

    def test_cpu_fallback_or_later_mapping_change_never_runs_encoder(self):
        clip = self.load()
        self.clip.patcher.load_device = Device('cpu')
        with self.assertRaisesRegex(RuntimeError, 'device_contract_changed'):
            clip.encode_from_tokens_scheduled('tokens')
        self.clip.patcher.load_device = Device('cuda:1')
        self.cuda.get_device_properties.side_effect = lambda i: SimpleNamespace(uuid=GPU0)
        with self.assertRaisesRegex(RuntimeError, 'mapping_mismatch'):
            clip.encode_from_tokens_scheduled('tokens')
        self.clip.encode_from_tokens_scheduled.assert_not_called()

    def test_workflow_variant_changes_only_encoder_node_from_deployed_cpu_graph(self):
        original = json.loads((ROOT / 'workflows/picture_api.json').read_text(encoding='utf-8'))
        variant = json.loads((ROOT / 'workflows/picture_api_gpu1_conditioning.json').read_text(encoding='utf-8'))
        cpu = dict(original)
        cpu.pop('95')  # Deployed CPU workflow already removed this unused node.
        self.assertEqual(set(variant), set(cpu))
        self.assertEqual(len(variant), 11)
        self.assertEqual({k: v for k, v in cpu.items() if k != '3'},
                         {k: v for k, v in variant.items() if k != '3'})
        self.assertEqual(variant['3']['class_type'], 'AIHubQwenGPU1CLIPLoader')
        self.assertEqual(variant['3']['inputs'], {
            'clip_name': cpu['3']['inputs']['clip_name'], 'gpu0_uuid': GPU0, 'gpu1_uuid': GPU1})
        self.assertEqual(original['3']['inputs']['device'], 'cpu')


if __name__ == '__main__':
    unittest.main()
