# Written by Dr. Hicham Badri @Mobius Labs GmbH - 2023
#####################################################
import torch
from torch import uint8, int32, float16, nn, Tensor
import copy
from enum import Enum
from typing import Union

from .utils import is_divisible, encode_safetensor_type, decode_safetensor_type
from .optimize import optimize_weights_proximal
from .bitpack import BitPack
from termcolor import colored

_META_TYPE = {
    "scale": torch.Tensor,
    "zero": torch.Tensor,
    "zero_scale": torch.Tensor,
    "compute_dtype": torch.dtype,
    "quant_zero": bool,
    "quant_scale": bool,
    "view_as_float": bool,
    "unpack_view_dtype": torch.dtype,
    "packing": str,
    "axis": int,
    "group_size": int,
    "nbits": int,
    "shape": torch.Size,
    "channel_wise": bool,
    "optimize": bool,
    "round_zero": bool,
}


# Main HQQ Quantizer
class Quantizer:
    SUPPORTED_BITS = [8, 6, 5, 4, 3, 2, 1.58, 1]
    optimize_weights = optimize_weights_proximal

    bit_to_packing = {
        8: "8bit_u8",
        6: "8bit_u8",  # todo: bitpacking
        5: "8bit_u8",  # todo: bitpacking
        4: "4bit_u8",
        3: "3bit_32",
        2: "2bit_u8",
        1.58: "2bit_u8",  # todo: bitpacking
        1: "1bit_u8",
    }

    pack = {
        "8bit_u8": BitPack.pack_8bit_u8,
        "4bit_u8": BitPack.pack_4bit_u8,
        "3bit_32": BitPack.pack_3bit_32,
        "2bit_u8": BitPack.pack_2bit_u8,
        "1bit_u8": BitPack.pack_1bit_u8,
    }

    unpack = {
        "8bit_u8": BitPack.unpack_8bit_u8,
        "4bit_u8": BitPack.unpack_4bit_u8,
        "3bit_32": BitPack.unpack_3bit_32,
        "2bit_u8": BitPack.unpack_2bit_u8,
        "1bit_u8": BitPack.unpack_1bit_u8,
    }

    unpack_view_dtype = {
        "8bit_u8": uint8,
        "4bit_u8": uint8,
        "3bit_32": int32,
        "2bit_u8": uint8,
        "1bit_u8": uint8,
    }

    @classmethod
    def quantize(
        cls,
        tensor: Tensor,
        nbits: float = 4,
        channel_wise: bool = True,
        group_size: int = 64,
        optimize: bool = True,
        round_zero: bool = False,
        axis: int = 0,
        bitpack: bool = True,
        compute_dtype: Union[torch.dtype, None] = None,
        view_as_float: bool = False,
        device: str = "cuda",
    ) -> tuple:
        assert nbits in Quantizer.SUPPORTED_BITS, (
            "nbits=" + str(nbits) + " not supported."
        )
        assert axis in [0, 1], "axis should be either 0 or 1"
        if group_size is not None:
            assert is_divisible(tensor.numel(), group_size), (
                "group_size should be divisble by the total tensor dimensions. shape: "
                + str(tensor.shape)
                + ", group_size: "
                + str(group_size)
            )

        W = tensor.float()
        shape = W.shape

        # Reshape for grouping
        if (group_size is not None) and channel_wise:
            W = (
                W.reshape([-1, group_size])
                if (axis == 1)
                else W.reshape([group_size, -1])
            )

        # Get min/max values
        if not channel_wise:
            _min, _max = W.min(), W.max()
            optimize = False
        else:
            _min = W.min(axis=axis, keepdim=True)[0]
            _max = W.max(axis=axis, keepdim=True)[0]

        max_v = round(2**nbits - 1)
        min_v = 0
        min_max = [min_v, max_v]

        # Note: here we work with the inverse of the scale to avoid division and quantize instead via W*scale + zero, the scale is inverted later on.
        denom = (_max - _min)
        scale = (max_v / denom)  
        scale = torch.where(denom.abs() <= 1e-4, torch.full_like(scale, 1.0), scale) #Avoid small denom values
        scale = scale.clamp(max=2e4) # clamp to avoid half-precision problems
        zero = -_min * scale

        # Round zero as in: https://github.com/casper-hansen/AutoAWQ/blob/main/awq/quantize/quantizer.py#L42C9-L42C14
        if round_zero:
            zero = torch.round(zero)

        # Fine-tune weights
        if optimize:
            W_q, scale, zero = Quantizer.optimize_weights(
                tensor=W,
                scale=scale,
                zero=zero,
                min_max=min_max,
                axis=axis,
                device=device,
            )
        else:
            W_q = (W * scale + zero).round_().clamp_(min_max[0], min_max[1])

        # cleanup
        del W, _min, _max
        torch.cuda.empty_cache()

        # Store meta-data (we invert the scale for dequantization)
        scale = 1.0 / scale
        meta = {
            "nbits": nbits,
            "group_size": group_size,
            "shape": shape,
            "scale": scale,
            "zero": zero,
            "axis": axis,
            "packing": Quantizer.bit_to_packing[nbits],
        }
        meta["unpack_view_dtype"] = Quantizer.unpack_view_dtype[meta["packing"]]

        # Pack bits
        meta["view_as_float"] = view_as_float
        if bitpack:
            W_q = Quantizer.pack[meta["packing"]](W_q)
            if view_as_float:
                W_q = W_q.view(
                    torch.float32 if compute_dtype is None else compute_dtype
                )  # store quantized weights as compute_dtype
        else:
            W_q = W_q.to(tensor.dtype)
            meta["packing"] = None

        torch.cuda.empty_cache()

        return W_q, meta

    # Main dequantization: bit_unpacking > (W_q - z)*s > reshape
    @classmethod
    def dequantize(cls, W_q: Tensor, meta: dict) -> Tensor:
        compute_dtype = meta["compute_dtype"] if ("compute_dtype" in meta) else float16
        if meta["packing"]:
            if meta["view_as_float"]:
                W_q = W_q.view(meta["unpack_view_dtype"])
            W_r = Quantizer.unpack[meta["packing"]](W_q, dtype=compute_dtype)
            if meta["nbits"] == 3:
                W_r = W_r[
                    : meta["group_size"]
                    if meta["axis"] == 0
                    else meta["shape"][0] * meta["shape"][1] // meta["group_size"]
                ]
        else:
            W_r = W_q.to(compute_dtype)
        W_r = ((W_r - meta["zero"]) * meta["scale"]).reshape(meta["shape"])
        return W_r

    @classmethod
    def to_inplace(cls, W_q: Tensor, meta: dict, device) -> tuple:
        compute_dtype = meta["compute_dtype"] if ("compute_dtype" in meta) else float16
        if W_q is not None:
            W_q = W_q.to(device).contiguous()
        for key in meta:
            if isinstance(meta[key], torch.Tensor):
                meta[key] = (
                    (
                        meta[key].to(compute_dtype)
                        if torch.is_floating_point(meta[key])
                        else meta[key]
                    )
                    .to(device)
                    .contiguous()
                )
        return W_q, meta

    @classmethod
    def to_ooplace(cls, W_q: Tensor, meta: dict, device) -> tuple:
        compute_dtype = meta["compute_dtype"] if ("compute_dtype" in meta) else float16
        if W_q is not None:
            W_q_c = W_q.to(device).contiguous()
        else:
            W_q_c = None
        meta_c = {}
        for key in meta:
            if isinstance(meta[key], torch.Tensor):
                meta_c[key] = (
                    (
                        meta[key].to(compute_dtype)
                        if torch.is_floating_point(meta[key])
                        else meta[key]
                    )
                    .to(device)
                    .contiguous()
                )
            else:
                meta_c[key] = meta[key]
        return W_q_c, meta_c

    @classmethod
    def cuda(cls, W_q: Tensor, meta: dict, device) -> tuple:
        return Quantizer.to_inplace(W_q, meta, device=device)

    @classmethod
    def cpu(cls, W_q: Tensor, meta: dict) -> tuple:
        return Quantizer.to_ooplace(W_q, meta, device="cpu")


# Check Aten/CUDA backend if available
hqq_aten_is_available = False
try:
    import hqq_aten
    hqq_aten_is_available = True

    @torch.library.custom_op("hqq::hqq_aten_dequantize", mutates_args=())
    def hqq_aten_dequantize(W_q: Tensor, scale: Tensor , zero:Tensor, N: int, K: int, group_size: int, nbits: int, axis: int, packing: str) -> Tensor:
        return hqq_aten.dequantize(W_q, scale, zero, N, K, group_size, nbits, axis, packing)

    @torch.library.register_fake("hqq::hqq_aten_dequantize")
    def hqq_aten_dequantize_fake(W_q: Tensor, scale: Tensor , zero:Tensor, N: int, K: int, group_size: int, nbits: int, axis: int, packing: str) -> Tensor:
        return torch.empty((N, K), device=W_q.device, dtype=scale.dtype)

except Exception:
    hqq_aten = None
    hqq_aten_is_available = False

class HQQBackend(Enum):
    # Name of the forward functions
    PYTORCH = "forward_pytorch_backprop"
    PYTORCH_COMPILE = "forward_pytorch_backprop_compile"
    ATEN = "forward_aten_backprop"

    # Alias for backward compatibility
    PYTORCH_BACKPROP = "forward_pytorch_backprop"
    PYTORCH_BACKPROP_COMPILE = "forward_pytorch_backprop_compile"
    ATEN_BACKPROP = "forward_aten_backprop"

    PYTORCH_FORWARD = "forward_pytorch"
    PYTORCH_FORWARD_COMPILE = "forward_pytorch_compile"
    ATEN_FORWARD = "forward_aten"

    # Experimental
    ATEN_FORWARD_INT8 = "forward_aten_int8"


# No cache: less memory, slower
class HQQMatmulNoCacheDeq(torch.autograd.Function):
    @staticmethod
    def forward(x: Tensor, dequantize, bias: Tensor):
        out = torch.matmul(x, dequantize().t())
        if bias is not None:
            out += bias
        return out

    @staticmethod
    def setup_context(ctx, inputs, outputs):
        x, dequantize, bias = inputs
        ctx.save_for_backward(x, bias)
        ctx.dequantize = dequantize

    @staticmethod
    def backward(ctx, grad_output):
        x, bias = ctx.saved_tensors

        grad_input = grad_weight = grad_bias = None

        if ctx.needs_input_grad[0]:
            grad_input = torch.matmul(grad_output, ctx.dequantize())

        # weight grad for frozen quantized weights not defined
        # if ctx.needs_input_grad[1]:
        #   grad_weight = torch.matmul(grad_output.t(), x)

        if bias is not None and ctx.needs_input_grad[2]:
            grad_bias = grad_output.sum(0)

        return grad_input, grad_weight, grad_bias


class HQQMatmulNoCacheMul(torch.autograd.Function):
    @staticmethod
    def forward(x, matmul, bias):
        out = matmul(x, transpose=True)
        if bias is not None:
            out += bias
        return out

    @staticmethod
    def setup_context(ctx, inputs, outputs):
        x, matmul, bias = inputs
        ctx.save_for_backward(x, bias)
        ctx.matmul = matmul

    @staticmethod
    def backward(ctx, grad_output):
        x, bias = ctx.saved_tensors

        grad_input = grad_weight = grad_bias = None

        if ctx.needs_input_grad[0]:
            grad_input = ctx.matmul(grad_output, transpose=False)

        # weight grad for frozen quantized weights not defined
        # if ctx.needs_input_grad[1]:
        #   grad_weight = torch.matmul(grad_output.t(), x)

        if bias is not None and ctx.needs_input_grad[2]:
            grad_bias = grad_output.sum(0)

        return grad_input, grad_weight, grad_bias


# Cache dequantized tensor: Faster but needs more memory
class HQQMatmulCachedDeq(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, hqq_layer, bias):
        weight_tmp = hqq_layer.dequantize()
        out = torch.matmul(x, weight_tmp.t())
        if bias is not None:
            out += bias

        ctx.save_for_backward(x, bias, weight_tmp)
        return out

    @staticmethod
    def backward(ctx, grad_output):
        x, bias, weight_tmp = ctx.saved_tensors

        grad_input = grad_weight = grad_bias = None

        if ctx.needs_input_grad[0]:
            grad_input = torch.matmul(grad_output, weight_tmp)

        del weight_tmp

        if bias is not None and ctx.needs_input_grad[2]:
            grad_bias = grad_output.sum(0)

        return grad_input, grad_weight, grad_bias


# Main linear layer
PRINT_ZERO_SCALE_DEPRECATED = True
PRINT_ATEN_WARNING = True
class HQQLinear(nn.Module):
    # Default backend
    backend = HQQBackend.PYTORCH

    def __init__(
        self,
        linear_layer: Union[nn.Module, None],
        quant_config: dict,
        del_orig: bool = True,
        compute_dtype: torch.dtype = float16,
        device: str = "cuda",
        initialize: bool = True,
    ):
        super().__init__()
        self.ready = False
        self.in_gpu = False
        self.bias = None
        self.axis = None
        self.channel_wise = None
        self.device = device
        self.compute_dtype = compute_dtype
        self.quant_config = copy.deepcopy(quant_config)
        self.del_orig = del_orig
        self.offload_meta = (
            self.quant_config.pop("offload_meta")
            if (self.quant_config is not None)
            else None
        )

        self.set_backend(HQQLinear.backend)

        self.linear_layer = linear_layer
        self.W_q = None
        self.meta = None
        self.encoded_state_dict = (
            True  # This makes state_dict compatible with safetensors
        )

        if initialize:
            self.initialize()

    def is_initialized(self):
        return False if (None in [self.W_q, self.meta]) else True

    def initialize(self):
        global PRINT_ZERO_SCALE_DEPRECATED
        if self.linear_layer is not None:
            if(self.quant_config['scale_quant_params'] is not None or self.quant_config['zero_quant_params'] is not None):
                if(PRINT_ZERO_SCALE_DEPRECATED):
                    print(colored('Warning: Quantizing zeros/scales is deprecated. This setting will be ignored.'  , 'yellow'))
                    PRINT_ZERO_SCALE_DEPRECATED = False
                self.quant_config['scale_quant_params'] = None
                self.quant_config['zero_quant_params'] = None

            # Handle group_size==None
            if self.quant_config["weight_quant_params"]["group_size"] == None:
                self.quant_config["weight_quant_params"]["group_size"] = (
                    self.linear_layer.in_features
                    if (self.quant_config["weight_quant_params"]["axis"] == 1)
                    else self.linear_layer.out_features
                )
                
            self.quantize(self.linear_layer.weight.data, **self.quant_config)
            self.bias = (
                None
                if (self.linear_layer.bias is None)
                else self.linear_layer.bias.clone().to(
                    device=self.device, dtype=self.compute_dtype
                )
            )

            #Clear-up parameters
            if self.del_orig:
                for name, param in self.linear_layer.named_parameters():
                    setattr(self.linear_layer, name, None)
                del self.linear_layer
                torch.cuda.empty_cache()

    @classmethod
    def from_weights(
        cls,
        weight: Tensor,
        bias: Union[Tensor, None],
        quant_config: dict,
        compute_dtype: torch.dtype = float16,
        device: str = "cuda",
        del_orig: bool = True,
    ):
        dummy_linear = torch.nn.Linear(1, 1)
        dummy_linear.in_features = weight.shape[1]
        dummy_linear.out_features= weight.shape[0]
        dummy_linear.weight.data = weight
        dummy_linear.bias = bias

        return cls(
            dummy_linear,
            quant_config=quant_config,
            compute_dtype=compute_dtype,
            device=device,
            del_orig=del_orig,
        )

    def extra_repr(self) -> str:
        out = ""
        if hasattr(self, "meta"):
            if self.meta is not None:
                in_features, out_features = self.meta["shape"][::-1]
                out = f"in_features={in_features}, out_features={out_features}, bias={self.bias is not None}"
        return out

    # Set backends
    @classmethod
    def set_backend(cls, backend: HQQBackend):
        global PRINT_ATEN_WARNING
        if "aten" in backend.value and PRINT_ATEN_WARNING:
            if hqq_aten_is_available is False:
                print(
                    colored("ATEN/CUDA backend not availabe. Make sure you install the hqq_aten library.", "yellow")
                )
                return
            print(
                colored("Warning: the ATEN/CUDA backend only supports axis=0 and GPU runtime.", "yellow")
            )
            PRINT_ATEN_WARNING = False
        HQQLinear.backend = backend
        cls.forward = getattr(cls, backend.value)

    # TODO: rewrite this mess
    def cuda(self, device):
        self.meta["compute_dtype"] = self.compute_dtype

        if isinstance(self.W_q, nn.parameter.Parameter):
            self.W_q.data, self.meta = Quantizer.cuda(self.W_q.data, self.meta, device)
        else:
            self.W_q, self.meta = Quantizer.cuda(self.W_q, self.meta, device)

        if self.meta["quant_zero"]:
            if "zero_q" in self.meta:
                self.meta["zero_q"], self.meta["meta_zero"] = Quantizer.cuda(
                    self.meta["zero_q"], self.meta["meta_zero"], device
                )
            else:
                _, self.meta["meta_zero"] = Quantizer.cuda(
                    None, self.meta["meta_zero"], device
                )
        elif "zero" in self.meta:
            self.meta["zero"] = self.meta["zero"].to(device)

        if self.meta["quant_scale"]:
            if "scale_q" in self.meta:
                self.meta["scale_q"], self.meta["meta_scale"] = Quantizer.cuda(
                    self.meta["scale_q"], self.meta["meta_scale"], device
                )
            else:
                _, self.meta["meta_scale"] = Quantizer.cuda(
                    None, self.meta["meta_scale"], device
                )
        elif "scale" in self.meta:
            self.meta["scale"] = self.meta["scale"].to(device)

        # #Use zero/scale with streams for dequantization is faster than packing in "zero_scale"
        # for key in ["zero", "zero_q", "scale", "scale_q"]:
        #     if((key in self.meta) and self.offload_meta):
        #         self.meta[key] = self.meta[key].contiguous().cpu().pin_memory()

        if self.offload_meta:
            if "zero_scale" not in self.meta:
                if self.meta["quant_scale"] and self.meta["quant_zero"]:
                    self.meta["zero_scale"] = torch.stack(
                        (self.meta["zero_q"], self.meta["scale_q"])
                    )
                    del self.meta["scale_q"], self.meta["zero_q"]
                else:
                    self.meta["zero_scale"] = torch.stack(
                        (self.meta["zero"], self.meta["scale"])
                    ).to(self.compute_dtype)
                    del self.meta["scale"], self.meta["zero"]

            self.meta["zero_scale"] = (
                self.meta["zero_scale"].contiguous().cpu().pin_memory()
            )

        if self.bias is not None:
            if isinstance(self.bias, torch.nn.Parameter):
                self.bias.data = self.bias.data.to(
                    device=device, dtype=self.compute_dtype
                )
            if isinstance(self.bias, torch.Tensor):
                self.bias = self.bias.to(device=device, dtype=self.compute_dtype)

        self.W_q = nn.Parameter(self.W_q, requires_grad=False)
        self.device = device
        self.in_gpu = True

        torch.cuda.empty_cache()

        return self

    def to(self, *args, **kwargs):
        # TODO: later
        return self

    # TODO: later
    # def to_empty(self, device, recurse=True):
    #     return self.cuda(device)

    def type(self, dst_type):
        # TODO: later
        return self

    def half(self, *args, **kwargs):
        return self

    def bfloat16(self, *args, **kwargs):
        # TODO: later
        return self

    def float(self, *args, **kwargs):
        # TODO: later
        return self

    def double(self, *args, **kwargs):
        return self

    def cpu(self):
        # TODO: later
        return self

    # state_dict is encoded by default for safetensors support. You can get the raw dict by setting self.encoded_state_dict=False. \
    # Note: you can't change the state once it's done
    def state_dict_keys(self):
        return set(
            [
                "W_q",
                "nbits",
                "group_size",
                "shape",
                "scale",
                "zero",
                "axis",
                "packing",
                "unpack_view_dtype",
                "view_as_float",
                "quant_scale",
                "quant_zero",
                "compute_dtype",
                "bias",
                "offload_meta",
                "encoded_state_dict",
                "stores_quant_config",
                "channel_wise",
                "optimize",
                "round_zero",
            ]
        )

    def state_dict(self, *args, **kwargs):  # nn.Module override compatible
        if not self.is_initialized():
            return {k: None for k in self.state_dict_keys()}

        if (
            self.quant_config["scale_quant_params"]
            or self.quant_config["zero_quant_params"]
        ) and self.encoded_state_dict:
            raise Exception(
                "Unsupported serialization for quantized scale/zero and self.encoded_state_dict=True"
            )
            # TODO: add support for quantized zero/scale case (quant_config and zero/scale)

        _encode_type = (
            encode_safetensor_type if (self.encoded_state_dict) else lambda z: z
        )

        # Core data
        state = {"W_q": self.W_q}
        state.update({k: _encode_type(v) for k, v in self.meta.items()})

        if self.bias is not None:
            state["bias"] = self.bias
        state["offload_meta"] = _encode_type(self.offload_meta)

        # Encoding flag
        if self.encoded_state_dict:
            state["encoded_state_dict"] = _encode_type(self.encoded_state_dict)

        # Quant config
        state["stores_quant_config"] = _encode_type(True)
        for k in self.quant_config["weight_quant_params"]:
            state[k] = _encode_type(self.quant_config["weight_quant_params"][k])

        if "destination" in kwargs and "prefix" in kwargs:
            for key, value in state.items():
                kwargs["destination"][kwargs["prefix"] + key] = value
        return state


    def _load_from_state_dict(
        self,
        state_dict,
        prefix,
        local_metadata,
        strict,
        missing_keys,
        unexpected_keys,
        error_msgs,
    ):
        
        layer_state_dict = {}
        for key in self.state_dict_keys():
            if(prefix + key in state_dict):
                layer_state_dict[key] = state_dict.pop(prefix + key)
            else:
                if(key not in ['bias']):
                    missing_keys.append(prefix + key)                    

        if 'W_q' in layer_state_dict:
            layer_state_dict['W_q'] = nn.Parameter(layer_state_dict['W_q'], requires_grad=False)
            self.load_state_dict(layer_state_dict, strict=strict)
        else:
            missing_keys.append(prefix + "W_q")  

    def load_state_dict(self, state_dict, strict=True, assign=False):
        if "encoded_state_dict" in state_dict:
            encoded_state_dict = True
            state_dict.pop("encoded_state_dict")
        else:
            encoded_state_dict = False

        _decode_type = (
            decode_safetensor_type if (encoded_state_dict) else lambda z, w: z
        )

        # Quant-config
        if state_dict.pop(
            "stores_quant_config", False
        ):  # check for backward compatibility
            self.quant_config = {
                "weight_quant_params": {
                    k: _decode_type(state_dict[k], _META_TYPE[k])
                    for k in [
                        "nbits",
                        "channel_wise",
                        "group_size",
                        "optimize",
                        "round_zero",
                        "axis",
                        "view_as_float",
                    ]
                }
            }
            # TODO: scale/zero quant use-case
            self.quant_config["scale_quant_params"] = state_dict.pop(
                "scale_quant_params", None
            )
            self.quant_config["zero_quant_params"] = state_dict.pop(
                "zero_quant_params", None
            )

        # W_q/ bias
        self.W_q = state_dict.pop("W_q")
        self.bias = state_dict.pop("bias", None)

        # Meta
        self.offload_meta = _decode_type(state_dict.pop("offload_meta", False), bool)
        if "meta" in state_dict:
            self.meta = state_dict["meta"]  # Backward compatibility
        else:
            self.meta = {
                k: _decode_type(v, _META_TYPE[k]) for k, v in state_dict.items()
            }  # safetensors version

        # Meta-data offloading
        if self.offload_meta is None:
            self.offload_meta = False
        for key in ["zero", "zero_q", "scale", "scale_q", "zero_scale"]:
            if key in self.meta and self.offload_meta:
                self.meta[key] = self.meta[key].cpu().contiguous().pin_memory()

        # Float view settings
        if "unpack_view_dtype" not in self.meta:
            self.meta["unpack_view_dtype"] = Quantizer.unpack_view_dtype[
                self.meta["packing"]
            ]

        if "view_as_float" not in self.meta:
            self.meta["view_as_float"] = False

        if "meta_scale" in self.meta:
            if "view_as_float" not in self.meta["meta_scale"]:
                self.meta["meta_scale"]["view_as_float"] = False

        if "meta_zero" in self.meta:
            if "view_as_float" not in self.meta["meta_zero"]:
                self.meta["meta_zero"]["view_as_float"] = False

        # Check GPU
        self.cuda(self.device)
        self.ready = True

        # Set in_features/out_features
        self.in_features, self.out_features = self.meta["shape"][::-1]

    def quantize(
        self,
        W: Tensor,
        weight_quant_params: dict,
        scale_quant_params: dict,
        zero_quant_params: dict,
    ) -> None:
        quant_scale = scale_quant_params is not None
        quant_zero = zero_quant_params is not None

        self.in_features, self.out_features = W.t().shape

        # Quantize
        W_q, meta = Quantizer.quantize(
            W,
            device=self.device,
            compute_dtype=self.compute_dtype,
            **weight_quant_params,
        )
        meta.update({"quant_scale": quant_scale, "quant_zero": quant_zero})

        if meta["quant_zero"]:
            meta["zero_q"], meta["meta_zero"] = Quantizer.quantize(
                meta["zero"],
                device=self.device,
                view_as_float=False,
                **zero_quant_params,
            )
            del meta["zero"]
            meta["meta_zero"]["compute_dtype"] = self.compute_dtype

        if meta["quant_scale"]:
            meta["scale_q"], meta["meta_scale"] = Quantizer.quantize(
                meta["scale"],
                device=self.device,
                view_as_float=False,
                **scale_quant_params,
            )
            del meta["scale"]
            meta["meta_scale"]["compute_dtype"] = self.compute_dtype

        self.W_q = W_q
        self.meta = meta
        self.cuda(self.device)
        self.ready = True

    def unpack(self, reshape=False, dtype=None):
        if self.ready is False:
            return None
        if self.meta["packing"]:
            W_r = Quantizer.unpack[self.meta["packing"]](
                self.W_q, dtype=dtype if (dtype is not None) else self.compute_dtype
            )
            return W_r.view(self.meta["shape"]) if (reshape) else W_r

    def dequantize(self):
        assert self.ready, "model was not quantized"
        W_q, meta = self.W_q, self.meta
        device = W_q.device
        del_keys = set()

        # Zero/Scale packed together
        if "zero_scale" in meta:
            zero_scale = meta["zero_scale"].to(device=device)

            if zero_scale.dtype == uint8:
                meta["zero_q"], meta["scale_q"] = zero_scale[0], zero_scale[1]
                del_keys.update({"zero_q", "scale_q"})
            else:
                meta["zero"], meta["scale"] = zero_scale[0], zero_scale[1]
                del_keys.update({"zero", "scale"})

        if meta["quant_zero"]:
            meta["zero"] = Quantizer.dequantize(
                meta["zero_q"].to(device=device), meta["meta_zero"]
            )
            del_keys.add("zero")

        if meta["quant_scale"]:
            meta["scale"] = Quantizer.dequantize(
                meta["scale_q"].to(device=device), meta["meta_scale"]
            )
            del_keys.add("scale")

        W_est = Quantizer.dequantize(W_q, meta)

        # Cleanup
        for key in del_keys:
            del meta[key]
        return W_est

    def matmul(self, x: Tensor, transpose: bool = True) -> Tensor:
        weight = self.dequantize()
        return torch.matmul(x, weight.t() if (transpose) else weight)

    @torch.compile()
    def matmul_compile(self, *args, **kwargs):
        return self.matmul(*args, **kwargs)

    def forward_pytorch_backprop(self, x: Tensor) -> Tensor:
        return HQQMatmulNoCacheMul.apply(x, self.matmul, self.bias)

    def forward_pytorch_backprop_compile(self, x: Tensor) -> Tensor:
        return HQQMatmulNoCacheMul.apply(x, self.matmul_compile, self.bias)

    def forward_pytorch(self, x: Tensor) -> Tensor:
        out = torch.matmul(x, self.dequantize().t())
        if self.bias is not None:
            out += self.bias
        return out

    @torch.compile()
    def forward_pytorch_compile(self, x: Tensor) -> Tensor:
        return self.forward_pytorch(x)

    ############################################################################################
    # ATen C++ / CUDA Bacekdn
    ##########################################################################################
    # Requires building the aten backend
    def dequantize_Wq_aten(self, W_q: Tensor, meta: dict):
        if meta["view_as_float"]:
            W_q = W_q.view(meta["unpack_view_dtype"])

        N, K = meta["shape"]

        return hqq_aten_dequantize(
            W_q,
            meta["scale"],
            meta["zero"],
            N, K,
            meta["group_size"] if (meta["group_size"]) else -1,
            meta["nbits"],
            meta["axis"],
            meta["packing"],
        )

    def dequantize_aten(self):
        # Dequantize
        assert self.ready, "model was not quantized"
        assert (
            self.meta["axis"] == 0
        ), "only axis=0 is supported. Use HQQLinear.set_backend(HQQBackend.PYTORCH) instead."

        W_q, meta = self.W_q, self.meta
        device = W_q.device
        del_keys = set()

        # Zero/Scale packed together
        if "zero_scale" in meta:
            zero_scale = meta["zero_scale"].to(device=device, non_blocking=True)
            if zero_scale.dtype == uint8:
                meta["zero_q"], meta["scale_q"] = zero_scale[0], zero_scale[1]
                del_keys.update({"zero_q", "scale_q"})
            else:
                meta["zero"], meta["scale"] = zero_scale[0], zero_scale[1]
                del_keys.update({"zero", "scale"})

        # Dequantize zero_q / scale_q with device loading
        if meta["quant_zero"]:
            if meta["meta_zero"]["group_size"]:
                meta["zero"] = self.dequantize_Wq_aten(
                    meta["zero_q"].to(device=device), meta["meta_zero"]
                )
            else:
                meta["zero"] = Quantizer.dequantize(
                    meta["zero_q"].to(device=device), meta["meta_zero"]
                )
            del_keys.add("zero")

        if meta["quant_scale"]:
            if meta["meta_scale"]["group_size"]:
                meta["scale"] = self.dequantize_Wq_aten(
                    meta["scale_q"].to(device=device), meta["meta_scale"]
                )
            else:
                meta["scale"] = Quantizer.dequantize(
                    meta["scale_q"].to(device=device), meta["meta_scale"]
                )
            del_keys.add("scale")

        # Reconstruct the weights
        W_est = self.dequantize_Wq_aten(W_q, meta)

        # Cleanup
        for key in del_keys:
            del meta[key]

        return W_est

    # Much faster with data-offloading zero_q/scale_q but takes more VRAM
    def dequantize_aten_with_streams(self):
        # Create streams
        if hasattr(self, "stream_zero") is False:
            self.stream_zero = torch.cuda.Stream()
            self.stream_scale = torch.cuda.Stream()

        # Dequantize
        assert self.ready, "model was not quantized"
        W_q, meta = self.W_q, self.meta
        device = W_q.device
        del_keys = set()

        # Zero/Scale packed together
        if "zero_scale" in meta:
            zero_scale = meta["zero_scale"].to(device=device, non_blocking=True)
            if zero_scale.dtype == uint8:
                meta["zero_q"], meta["scale_q"] = zero_scale[0], zero_scale[1]
                del_keys.update({"zero_q", "scale_q"})
            else:
                meta["zero"], meta["scale"] = zero_scale[0], zero_scale[1]
                del_keys.update({"zero", "scale"})

        # Using non_blocking=False for the moment, otherwise it can result in strange behaviors
        non_blocking = False
        with torch.cuda.stream(self.stream_zero):
            if meta["quant_zero"]:
                if meta["meta_zero"]["group_size"]:
                    meta["zero"] = self.dequantize_Wq_aten(
                        meta["zero_q"].to(device=device, non_blocking=non_blocking),
                        meta["meta_zero"],
                    )
                else:
                    meta["zero"] = Quantizer.dequantize(
                        meta["zero_q"].to(device=device, non_blocking=non_blocking),
                        meta["meta_zero"],
                    )
                del_keys.add("zero")

        with torch.cuda.stream(self.stream_scale):
            if meta["quant_scale"]:
                if meta["meta_scale"]["group_size"]:
                    meta["scale"] = self.dequantize_Wq_aten(
                        meta["scale_q"].to(device=device, non_blocking=non_blocking),
                        meta["meta_scale"],
                    )
                else:
                    meta["scale"] = Quantizer.dequantize(
                        meta["scale_q"].to(device=device, non_blocking=non_blocking),
                        meta["meta_scale"],
                    )
                del_keys.add("scale")

        # Wait for streams to finish
        torch.cuda.synchronize()

        # Reconstruct the weights
        W_est = self.dequantize_Wq_aten(W_q, meta)

        # Cleanup
        for key in del_keys:
            del meta[key]

        return W_est

    def forward_aten(self, x: Tensor) -> Tensor:
        W_est = self.dequantize_aten()
        out = torch.matmul(x, W_est.t())
        if self.bias is not None:
            out += self.bias

        return out

    def forward_aten_backprop(self, x: Tensor) -> Tensor:
        return HQQMatmulNoCacheDeq.apply(x, self.dequantize_aten, self.bias)

    # TODO: as fused kernel in CUDA
    def _get_int8_matrix(self, M):
        scale = torch.abs(M).amax() / 127.0
        return torch.round(M / scale).to(torch.int8), scale.float()

    # TODO: in ATEN
    @torch.compile()
    def _matmul_int8(self, A, B):
        dtype = A.dtype
        A, scale_A = self._get_int8_matrix(A)
        B, scale_B = self._get_int8_matrix(B)
        return (torch._int_mm(A, B) * (scale_A * scale_B)).to(dtype)

    def forward_aten_int8(self, x: Tensor) -> Tensor:
        W_est = self.dequantize_aten()
        out = self._matmul_int8(x[0], W_est.t())[None, ...]
        if self.bias is not None:
            out += self.bias

        return out


def hqq_base_quant_config(
    nbits: int = 4,
    group_size: int = 64,
    quant_zero: bool = False,
    quant_scale: bool = False,
    offload_meta: bool = False,  # meta-data should be quantized with the same settings to use offload_meta
    view_as_float: bool = False,
    axis: int = 1,
):
    assert (
        nbits in Quantizer.SUPPORTED_BITS
    ), "nbits value not supported. Check Quantizer.SUPPORTED_BITS."
    if group_size is not None:
        assert is_divisible(
            group_size, 8
        ), "Invalid group_size param: the value should be a multiple of 8."
    weight_quant_params = {
        "nbits": nbits,
        "channel_wise": True,
        "group_size": group_size,
        "optimize": True,
        "round_zero": True if nbits == 4 else False,
        "axis": axis,
        "view_as_float": view_as_float,
    }

    if quant_zero or quant_scale:
        print(
            colored(
                "Warning: Quantized meta-data is deprecated and will be removed. It is not supported for quantized model serialization.",
                "yellow",
            )
        )

    if offload_meta:
        print(
            colored(
                "Warning: Meta-data offloading is deprecated and will be removed. It is not supported for quantized model serialization.",
                "yellow",
            )
        )

    if offload_meta:
        if quant_scale != quant_zero:
            # print(colored("quant_zero and quant_scale must be the same when offload_meta is set to True. Setting quant_scale=quant_zero." , 'yellow'))
            quant_scale = quant_zero

        scale_quant_params = (
            {"nbits": 8, "channel_wise": True, "group_size": 128, "optimize": False}
            if (quant_scale)
            else None
        )
        zero_quant_params = (
            {"nbits": 8, "channel_wise": True, "group_size": 128, "optimize": False}
            if (quant_zero)
            else None
        )

    else:
        scale_quant_params = (
            {"nbits": 8, "channel_wise": True, "group_size": 128, "optimize": False}
            if (quant_scale)
            else None
        )
        zero_quant_params = (
            {"nbits": 8, "channel_wise": False, "group_size": None, "optimize": False}
            if (quant_zero)
            else None
        )

    return {
        "weight_quant_params": weight_quant_params,
        "scale_quant_params": scale_quant_params,
        "zero_quant_params": zero_quant_params,
        "offload_meta": offload_meta,
    }


# Alias: follow similar Auto-GPTQ naming
BaseQuantizeConfig = hqq_base_quant_config
