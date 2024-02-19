"""
https://github.com/feinanshan/M2M_VFI/blob/main/Test/model/py
https://raw.githubusercontent.com/feinanshan/M2M_VFI/main/Test/model/py
https://github.com/feinanshan/M2M_VFI/blob/main/Test/model/py
https://github.com/feinanshan/M2M_VFI/blob/main/Test/model/py
https://github.com/feinanshan/M2M_VFI/blob/main/Test/model/m2m.py
"""

import collections
import cupy
import math
import os
import re
import torch
import typing

##########################################################


objBackwarpcache = {}


def backwarp(tenIn: torch.Tensor, tenFlow: torch.Tensor):
    if (
        "grid"
        + str(tenFlow.dtype)
        + str(tenFlow.device)
        + str(tenFlow.shape[2])
        + str(tenFlow.shape[3])
        not in objBackwarpcache
    ):
        tenHor = (
            torch.linspace(
                start=-1.0,
                end=1.0,
                steps=tenFlow.shape[3],
                dtype=tenFlow.dtype,
                device=tenFlow.device,
            )
            .view(1, 1, 1, -1)
            .repeat(1, 1, tenFlow.shape[2], 1)
        )
        tenVer = (
            torch.linspace(
                start=-1.0,
                end=1.0,
                steps=tenFlow.shape[2],
                dtype=tenFlow.dtype,
                device=tenFlow.device,
            )
            .view(1, 1, -1, 1)
            .repeat(1, 1, 1, tenFlow.shape[3])
        )

        objBackwarpcache[
            "grid"
            + str(tenFlow.dtype)
            + str(tenFlow.device)
            + str(tenFlow.shape[2])
            + str(tenFlow.shape[3])
        ] = torch.cat([tenHor, tenVer], 1)
    # end

    if tenFlow.shape[3] == tenFlow.shape[2]:
        tenFlow = tenFlow * (2.0 / ((tenFlow.shape[3] and tenFlow.shape[2]) - 1.0))

    elif tenFlow.shape[3] != tenFlow.shape[2]:
        tenFlow = tenFlow * torch.tensor(
            data=[2.0 / (tenFlow.shape[3] - 1.0), 2.0 / (tenFlow.shape[2] - 1.0)],
            dtype=tenFlow.dtype,
            device=tenFlow.device,
        ).view(1, 2, 1, 1)

    # end

    return torch.nn.functional.grid_sample(
        input=tenIn,
        grid=(
            objBackwarpcache[
                "grid"
                + str(tenFlow.dtype)
                + str(tenFlow.device)
                + str(tenFlow.shape[2])
                + str(tenFlow.shape[3])
            ]
            + tenFlow
        ).permute(0, 2, 3, 1),
        mode="bilinear",
        padding_mode="zeros",
        align_corners=True,
    )


# end


##########################################################


objCudacache = {}


def cuda_int32(intIn: int):
    return cupy.int32(intIn)


# end


def cuda_float32(fltIn: float):
    return cupy.float32(fltIn)


# end


def cuda_kernel(strFunction: str, strKernel: str, objVariables: typing.Dict):
    if "device" not in objCudacache:
        objCudacache["device"] = torch.cuda.get_device_name()
    # end

    strKey = strFunction

    for strVariable in objVariables:
        objValue = objVariables[strVariable]

        strKey += strVariable

        if objValue is None:
            continue

        elif type(objValue) == int:
            strKey += str(objValue)

        elif type(objValue) == float:
            strKey += str(objValue)

        elif type(objValue) == bool:
            strKey += str(objValue)

        elif type(objValue) == str:
            strKey += objValue

        elif type(objValue) == torch.Tensor:
            strKey += str(objValue.dtype)
            strKey += str(objValue.shape)
            strKey += str(objValue.stride())

        elif True:
            print(strVariable, type(objValue))
            assert False

        # end
    # end

    strKey += objCudacache["device"]

    if strKey not in objCudacache:
        for strVariable in objVariables:
            objValue = objVariables[strVariable]

            if objValue is None:
                continue

            elif type(objValue) == int:
                strKernel = strKernel.replace("{{" + strVariable + "}}", str(objValue))

            elif type(objValue) == float:
                strKernel = strKernel.replace("{{" + strVariable + "}}", str(objValue))

            elif type(objValue) == bool:
                strKernel = strKernel.replace("{{" + strVariable + "}}", str(objValue))

            elif type(objValue) == str:
                strKernel = strKernel.replace("{{" + strVariable + "}}", objValue)

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.uint8:
                strKernel = strKernel.replace("{{type}}", "unsigned char")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.float16:
                strKernel = strKernel.replace("{{type}}", "half")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.float32:
                strKernel = strKernel.replace("{{type}}", "float")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.float64:
                strKernel = strKernel.replace("{{type}}", "double")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.int32:
                strKernel = strKernel.replace("{{type}}", "int")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.int64:
                strKernel = strKernel.replace("{{type}}", "long")

            elif type(objValue) == torch.Tensor:
                print(strVariable, objValue.dtype)
                assert False

            elif True:
                print(strVariable, type(objValue))
                assert False

            # end
        # end

        while True:
            objMatch = re.search("(SIZE_)([0-4])(\()([^\)]*)(\))", strKernel)

            if objMatch is None:
                break
            # end

            intArg = int(objMatch.group(2))

            strTensor = objMatch.group(4)
            intSizes = objVariables[strTensor].size()

            strKernel = strKernel.replace(
                objMatch.group(),
                str(
                    intSizes[intArg]
                    if torch.is_tensor(intSizes[intArg]) == False
                    else intSizes[intArg].item()
                ),
            )
        # end

        while True:
            objMatch = re.search("(OFFSET_)([0-4])(\()", strKernel)

            if objMatch is None:
                break
            # end

            intStart = objMatch.span()[1]
            intStop = objMatch.span()[1]
            intParentheses = 1

            while True:
                intParentheses += 1 if strKernel[intStop] == "(" else 0
                intParentheses -= 1 if strKernel[intStop] == ")" else 0

                if intParentheses == 0:
                    break
                # end

                intStop += 1
            # end

            intArgs = int(objMatch.group(2))
            strArgs = strKernel[intStart:intStop].split(",")

            assert intArgs == len(strArgs) - 1

            strTensor = strArgs[0]
            intStrides = objVariables[strTensor].stride()

            strIndex = []

            for intArg in range(intArgs):
                strIndex.append(
                    "(("
                    + strArgs[intArg + 1].replace("{", "(").replace("}", ")").strip()
                    + ")*"
                    + str(
                        intStrides[intArg]
                        if torch.is_tensor(intStrides[intArg]) == False
                        else intStrides[intArg].item()
                    )
                    + ")"
                )
            # end

            strKernel = strKernel.replace(
                "OFFSET_" + str(intArgs) + "(" + strKernel[intStart:intStop] + ")",
                "(" + str.join("+", strIndex) + ")",
            )
        # end

        while True:
            objMatch = re.search("(VALUE_)([0-4])(\()", strKernel)

            if objMatch is None:
                break
            # end

            intStart = objMatch.span()[1]
            intStop = objMatch.span()[1]
            intParentheses = 1

            while True:
                intParentheses += 1 if strKernel[intStop] == "(" else 0
                intParentheses -= 1 if strKernel[intStop] == ")" else 0

                if intParentheses == 0:
                    break
                # end

                intStop += 1
            # end

            intArgs = int(objMatch.group(2))
            strArgs = strKernel[intStart:intStop].split(",")

            assert intArgs == len(strArgs) - 1

            strTensor = strArgs[0]
            intStrides = objVariables[strTensor].stride()

            strIndex = []

            for intArg in range(intArgs):
                strIndex.append(
                    "(("
                    + strArgs[intArg + 1].replace("{", "(").replace("}", ")").strip()
                    + ")*"
                    + str(
                        intStrides[intArg]
                        if torch.is_tensor(intStrides[intArg]) == False
                        else intStrides[intArg].item()
                    )
                    + ")"
                )
            # end

            strKernel = strKernel.replace(
                "VALUE_" + str(intArgs) + "(" + strKernel[intStart:intStop] + ")",
                strTensor + "[" + str.join("+", strIndex) + "]",
            )
        # end

        objCudacache[strKey] = {"strFunction": strFunction, "strKernel": strKernel}
    # end

    return strKey


# end


@cupy.memoize(for_each_device=True)
def cuda_launch(strKey: str):
    if "CUDA_HOME" not in os.environ:
        os.environ["CUDA_HOME"] = "/usr/local/cuda/"
    # end

    return cupy.cuda.compile_with_cache(
        objCudacache[strKey]["strKernel"],
        tuple(
            [
                "-I " + os.environ["CUDA_HOME"],
                "-I " + os.environ["CUDA_HOME"] + "/include",
            ]
        ),
    ).get_function(objCudacache[strKey]["strFunction"])


# end


##########################################################


class costvol_func(torch.autograd.Function):
    @staticmethod
    @torch.cuda.amp.custom_fwd(cast_inputs=torch.float32)
    def forward(self, tenOne, tenTwo):
        tenOut = tenOne.new_empty(
            [tenOne.shape[0], 81, tenOne.shape[2], tenOne.shape[3]]
        )

        cuda_launch(
            cuda_kernel(
                "costvol_out",
                """
            extern "C" __global__ void __launch_bounds__(512) costvol_out(
                const int n,
                const {{type}}* __restrict__ tenOne,
                const {{type}}* __restrict__ tenTwo,
                {{type}}* __restrict__ tenOut
            ) { for (int intIndex = (blockIdx.x * blockDim.x) + threadIdx.x; intIndex < n; intIndex += blockDim.x * gridDim.x) {
                const int intN = ( intIndex / SIZE_3(tenOut) / SIZE_2(tenOut) ) % SIZE_0(tenOut);
                const int intC = -1;
                const int intY = ( intIndex / SIZE_3(tenOut)                  ) % SIZE_2(tenOut);
                const int intX = ( intIndex                                   ) % SIZE_3(tenOut);

                {{type}} fltOne[{{intChans}}];

                for (int intValue = 0; intValue < SIZE_1(tenOne); intValue += 1) {
                    fltOne[intValue] = VALUE_4(tenOne, intN, intValue, intY, intX);
                }

                int intOffset = OFFSET_4(tenOut, intN, 0, intY, intX);

                for (int intOy = intY - 4; intOy <= intY + 4; intOy += 1) {
                    for (int intOx = intX - 4; intOx <= intX + 4; intOx += 1) {
                        {{type}} fltValue = 0.0f;

                        if ((intOy >= 0) && (intOy < SIZE_2(tenOut)) && (intOx >= 0) && (intOx < SIZE_3(tenOut))) {
                            for (int intValue = 0; intValue < SIZE_1(tenOne); intValue += 1) {
                                fltValue += abs(fltOne[intValue] - VALUE_4(tenTwo, intN, intValue, intOy, intOx));
                            }
                        } else {
                            for (int intValue = 0; intValue < SIZE_1(tenOne); intValue += 1) {
                                fltValue += abs(fltOne[intValue]);
                            }
                        }

                        tenOut[intOffset] = fltValue / SIZE_1(tenOne);
                        intOffset += SIZE_2(tenOut) * SIZE_3(tenOut);
                    }
                }
            } }
        """,
                {
                    "intChans": tenOne.shape[1],
                    "tenOne": tenOne,
                    "tenTwo": tenTwo,
                    "tenOut": tenOut,
                },
            )
        )(
            grid=tuple(
                [
                    int(
                        (
                            (tenOut.shape[0] * tenOut.shape[2] * tenOut.shape[3])
                            + 512
                            - 1
                        )
                        / 512
                    ),
                    1,
                    1,
                ]
            ),
            block=tuple([512, 1, 1]),
            args=[
                cuda_int32(tenOut.shape[0] * tenOut.shape[2] * tenOut.shape[3]),
                tenOne.data_ptr(),
                tenTwo.data_ptr(),
                tenOut.data_ptr(),
            ],
            stream=collections.namedtuple("Stream", "ptr")(
                torch.cuda.current_stream().cuda_stream
            ),
        )

        self.save_for_backward(tenOne, tenTwo)

        return tenOut

    # end

    @staticmethod
    @torch.cuda.amp.custom_bwd
    def backward(self, tenOutgrad):
        tenOne, tenTwo = self.saved_tensors

        tenOutgrad = tenOutgrad.contiguous()
        assert tenOutgrad.is_cuda == True

        tenOnegrad = (
            tenOne.new_zeros(
                [tenOne.shape[0], tenOne.shape[1], tenOne.shape[2], tenOne.shape[3]]
            )
            if self.needs_input_grad[0] == True
            else None
        )
        tenTwograd = (
            tenTwo.new_zeros(
                [tenTwo.shape[0], tenTwo.shape[1], tenTwo.shape[2], tenTwo.shape[3]]
            )
            if self.needs_input_grad[1] == True
            else None
        )

        if tenOnegrad is not None:
            cuda_launch(
                cuda_kernel(
                    "costvol_onegrad",
                    """
                extern "C" __global__ void __launch_bounds__(512) costvol_onegrad(
                    const int n,
                    const {{type}}* __restrict__ tenOne,
                    const {{type}}* __restrict__ tenTwo,
                    const {{type}}* __restrict__ tenOutgrad,
                    {{type}}* __restrict__ tenOnegrad,
                    {{type}}* __restrict__ tenTwograd
                ) { for (int intIndex = (blockIdx.x * blockDim.x) + threadIdx.x; intIndex < n; intIndex += blockDim.x * gridDim.x) {
                    const int intN = ( intIndex / SIZE_3(tenOnegrad) / SIZE_2(tenOnegrad) ) % SIZE_0(tenOnegrad);
                    const int intC = -1;
                    const int intY = ( intIndex / SIZE_3(tenOnegrad)                      ) % SIZE_2(tenOnegrad);
                    const int intX = ( intIndex                                           ) % SIZE_3(tenOnegrad);

                    {{type}} fltOne[{{intChans}}];

                    for (int intValue = 0; intValue < SIZE_1(tenOne); intValue += 1) {
                        fltOne[intValue] = VALUE_4(tenOne, intN, intValue, intY, intX);
                    }

                    int intOffset = OFFSET_4(tenOutgrad, intN, 0, intY, intX);

                    for (int intOy = intY - 4; intOy <= intY + 4; intOy += 1) {
                        for (int intOx = intX - 4; intOx <= intX + 4; intOx += 1) {
                            if ((intOy >= 0) && (intOy < SIZE_2(tenOutgrad)) && (intOx >= 0) && (intOx < SIZE_3(tenOutgrad))) {
                                for (int intValue = 0; intValue < SIZE_1(tenOne); intValue += 1) {
                                    if (fltOne[intValue] - VALUE_4(tenTwo, intN, intValue, intOy, intOx) >= 0.0f) {
                                        tenOnegrad[OFFSET_4(tenOnegrad, intN, intValue, intY, intX)] += +tenOutgrad[intOffset] / SIZE_1(tenOne);
                                    } else {
                                        tenOnegrad[OFFSET_4(tenOnegrad, intN, intValue, intY, intX)] += -tenOutgrad[intOffset] / SIZE_1(tenOne);
                                    }
                                }
                            } else {
                                for (int intValue = 0; intValue < SIZE_1(tenOne); intValue += 1) {
                                    if (fltOne[intValue] >= 0.0f) {
                                        tenOnegrad[OFFSET_4(tenOnegrad, intN, intValue, intY, intX)] += +tenOutgrad[intOffset] / SIZE_1(tenOne);
                                    } else {
                                        tenOnegrad[OFFSET_4(tenOnegrad, intN, intValue, intY, intX)] += -tenOutgrad[intOffset] / SIZE_1(tenOne);
                                    }
                                }
                            }

                            intOffset += SIZE_2(tenOutgrad) * SIZE_3(tenOutgrad);
                        }
                    }
                } }
            """,
                    {
                        "intChans": tenOne.shape[1],
                        "tenOne": tenOne,
                        "tenTwo": tenTwo,
                        "tenOutgrad": tenOutgrad,
                        "tenOnegrad": tenOnegrad,
                        "tenTwograd": tenTwograd,
                    },
                )
            )(
                grid=tuple(
                    [
                        int(
                            (
                                (
                                    tenOnegrad.shape[0]
                                    * tenOnegrad.shape[2]
                                    * tenOnegrad.shape[3]
                                )
                                + 512
                                - 1
                            )
                            / 512
                        ),
                        1,
                        1,
                    ]
                ),
                block=tuple([512, 1, 1]),
                args=[
                    cuda_int32(
                        tenOnegrad.shape[0] * tenOnegrad.shape[2] * tenOnegrad.shape[3]
                    ),
                    tenOne.data_ptr(),
                    tenTwo.data_ptr(),
                    tenOutgrad.data_ptr(),
                    tenOnegrad.data_ptr(),
                    tenTwograd.data_ptr(),
                ],
                stream=collections.namedtuple("Stream", "ptr")(
                    torch.cuda.current_stream().cuda_stream
                ),
            )
        # end

        if tenTwograd is not None:
            cuda_launch(
                cuda_kernel(
                    "costvol_twograd",
                    """
                extern "C" __global__ void __launch_bounds__(512) costvol_twograd(
                    const int n,
                    const {{type}}* __restrict__ tenOne,
                    const {{type}}* __restrict__ tenTwo,
                    const {{type}}* __restrict__ tenOutgrad,
                    {{type}}* __restrict__ tenOnegrad,
                    {{type}}* __restrict__ tenTwograd
                ) { for (int intIndex = (blockIdx.x * blockDim.x) + threadIdx.x; intIndex < n; intIndex += blockDim.x * gridDim.x) {
                    const int intN = ( intIndex / SIZE_3(tenTwograd) / SIZE_2(tenTwograd) ) % SIZE_0(tenTwograd);
                    const int intC = -1;
                    const int intY = ( intIndex / SIZE_3(tenTwograd)                      ) % SIZE_2(tenTwograd);
                    const int intX = ( intIndex                                           ) % SIZE_3(tenTwograd);

                    {{type}} fltOne[{{intChans}}];

                    for (int intValue = 0; intValue < SIZE_1(tenOne); intValue += 1) {
                        fltOne[intValue] = VALUE_4(tenOne, intN, intValue, intY, intX);
                    }

                    int intOffset = OFFSET_4(tenOutgrad, intN, 0, intY, intX);

                    for (int intOy = intY - 4; intOy <= intY + 4; intOy += 1) {
                        for (int intOx = intX - 4; intOx <= intX + 4; intOx += 1) {
                            if ((intOy >= 0) && (intOy < SIZE_2(tenOutgrad)) && (intOx >= 0) && (intOx < SIZE_3(tenOutgrad))) {
                                for (int intValue = 0; intValue < SIZE_1(tenOne); intValue += 1) {
                                    if (fltOne[intValue] - VALUE_4(tenTwo, intN, intValue, intOy, intOx) >= 0.0f) {
                                        atomicAdd(&tenTwograd[OFFSET_4(tenTwograd, intN, intValue, intOy, intOx)], -tenOutgrad[intOffset] / SIZE_1(tenOne));
                                    } else {
                                        atomicAdd(&tenTwograd[OFFSET_4(tenTwograd, intN, intValue, intOy, intOx)], +tenOutgrad[intOffset] / SIZE_1(tenOne));
                                    }
                                }
                            } else {
                                // ...
                            }

                            intOffset += SIZE_2(tenOutgrad) * SIZE_3(tenOutgrad);
                        }
                    }
                } }
            """,
                    {
                        "intChans": tenOne.shape[1],
                        "tenOne": tenOne,
                        "tenTwo": tenTwo,
                        "tenOutgrad": tenOutgrad,
                        "tenOnegrad": tenOnegrad,
                        "tenTwograd": tenTwograd,
                    },
                )
            )(
                grid=tuple(
                    [
                        int(
                            (
                                (
                                    tenTwograd.shape[0]
                                    * tenTwograd.shape[2]
                                    * tenTwograd.shape[3]
                                )
                                + 512
                                - 1
                            )
                            / 512
                        ),
                        1,
                        1,
                    ]
                ),
                block=tuple([512, 1, 1]),
                args=[
                    cuda_int32(
                        tenTwograd.shape[0] * tenTwograd.shape[2] * tenTwograd.shape[3]
                    ),
                    tenOne.data_ptr(),
                    tenTwo.data_ptr(),
                    tenOutgrad.data_ptr(),
                    tenOnegrad.data_ptr(),
                    tenTwograd.data_ptr(),
                ],
                stream=collections.namedtuple("Stream", "ptr")(
                    torch.cuda.current_stream().cuda_stream
                ),
            )
        # end

        return tenOnegrad, tenTwograd, None, None

    # end


# end


##########################################################


class Basic(torch.nn.Module):
    def __init__(
        self,
        strType: str,
        intChans: typing.List[int],
        objScratch: typing.Optional[typing.Dict] = None,
    ):
        super().__init__()

        self.strType = strType
        self.netEvenize = None
        self.netMain = None
        self.netShortcut = None

        intIn = intChans[0]
        intOut = intChans[-1]
        netMain = []
        intChans = intChans.copy()
        fltStride = 1.0

        for intPart, strPart in enumerate(self.strType.split("+")[0].split("-")):
            if strPart.startswith("evenize") == True and intPart == 0:

                class Evenize(torch.nn.Module):
                    def __init__(self, strPad):
                        super().__init__()

                        self.strPad = strPad

                    # end

                    def forward(self, tenIn: torch.Tensor) -> torch.Tensor:
                        intPad = [0, 0, 0, 0]

                        if tenIn.shape[3] % 2 != 0:
                            intPad[1] = 1
                        if tenIn.shape[2] % 2 != 0:
                            intPad[3] = 1

                        if min(intPad) != 0 or max(intPad) != 0:
                            tenIn = torch.nn.functional.pad(
                                input=tenIn,
                                pad=intPad,
                                mode=self.strPad
                                if self.strPad != "zeros"
                                else "constant",
                                value=0.0,
                            )
                        # end

                        return tenIn

                    # end

                # end

                strPad = "zeros"

                if "(" in strPart:
                    if "replpad" in strPart.split("(")[1].split(")")[0].split(","):
                        strPad = "replicate"
                    if "reflpad" in strPart.split("(")[1].split(")")[0].split(","):
                        strPad = "reflect"
                # end

                self.netEvenize = Evenize(strPad)

            elif strPart.startswith("conv") == True:
                intKsize = 3
                intPad = 1
                strPad = "zeros"

                if "(" in strPart:
                    intKsize = int(strPart.split("(")[1].split(")")[0].split(",")[0])
                    intPad = int(math.floor(0.5 * (intKsize - 1)))

                    if "replpad" in strPart.split("(")[1].split(")")[0].split(","):
                        strPad = "replicate"
                    if "reflpad" in strPart.split("(")[1].split(")")[0].split(","):
                        strPad = "reflect"
                # end

                if "nopad" in self.strType.split("+"):
                    intPad = 0
                # end

                netMain += [
                    torch.nn.Conv2d(
                        in_channels=intChans[0],
                        out_channels=intChans[1],
                        kernel_size=intKsize,
                        stride=1,
                        padding=intPad,
                        padding_mode=strPad,
                        bias="nobias" not in self.strType.split("+"),
                    )
                ]
                intChans = intChans[1:]
                fltStride *= 1.0

            elif strPart.startswith("sconv") == True:
                intKsize = 3
                intPad = 1
                strPad = "zeros"

                if "(" in strPart:
                    intKsize = int(strPart.split("(")[1].split(")")[0].split(",")[0])
                    intPad = int(math.floor(0.5 * (intKsize - 1)))

                    if "replpad" in strPart.split("(")[1].split(")")[0].split(","):
                        strPad = "replicate"
                    if "reflpad" in strPart.split("(")[1].split(")")[0].split(","):
                        strPad = "reflect"
                # end

                if "nopad" in self.strType.split("+"):
                    intPad = 0
                # end

                netMain += [
                    torch.nn.Conv2d(
                        in_channels=intChans[0],
                        out_channels=intChans[1],
                        kernel_size=intKsize,
                        stride=2,
                        padding=intPad,
                        padding_mode=strPad,
                        bias="nobias" not in self.strType.split("+"),
                    )
                ]
                intChans = intChans[1:]
                fltStride *= 2.0

            elif strPart.startswith("up") == True:

                class Up(torch.nn.Module):
                    def __init__(self, strType):
                        super().__init__()

                        self.strType = strType

                    # end

                    def forward(self, tenIn: torch.Tensor) -> torch.Tensor:
                        if self.strType == "nearest":
                            return torch.nn.functional.interpolate(
                                input=tenIn,
                                scale_factor=2.0,
                                mode="nearest-exact",
                                align_corners=False,
                            )

                        elif self.strType == "bilinear":
                            return torch.nn.functional.interpolate(
                                input=tenIn,
                                scale_factor=2.0,
                                mode="bilinear",
                                align_corners=False,
                            )

                        elif self.strType == "pyramid":
                            return pyramid(tenIn, None, "up")

                        elif self.strType == "shuffle":
                            return torch.nn.functional.pixel_shuffle(
                                tenIn, upscale_factor=2
                            )  # https://github.com/pytorch/pytorch/issues/62854

                        # end

                        assert False  # to make torchscript happy

                    # end

                # end

                strType = "bilinear"

                if "(" in strPart:
                    if "nearest" in strPart.split("(")[1].split(")")[0].split(","):
                        strType = "nearest"
                    if "pyramid" in strPart.split("(")[1].split(")")[0].split(","):
                        strType = "pyramid"
                    if "shuffle" in strPart.split("(")[1].split(")")[0].split(","):
                        strType = "shuffle"
                # end

                netMain += [Up(strType)]
                fltStride *= 0.5

            elif strPart.startswith("prelu") == True:
                netMain += [
                    torch.nn.PReLU(
                        num_parameters=1,
                        init=float(strPart.split("(")[1].split(")")[0].split(",")[0]),
                    )
                ]
                fltStride *= 1.0

            elif True:
                assert False

            # end
        # end

        self.netMain = torch.nn.Sequential(*netMain)

        for strPart in self.strType.split("+")[1:]:
            if strPart.startswith("skip") == True:
                if intIn == intOut and fltStride == 1.0:
                    self.netShortcut = torch.nn.Identity()

                elif intIn != intOut and fltStride == 1.0:
                    self.netShortcut = torch.nn.Conv2d(
                        in_channels=intIn,
                        out_channels=intOut,
                        kernel_size=1,
                        stride=1,
                        padding=0,
                        bias="nobias" not in self.strType.split("+"),
                    )

                elif intIn == intOut and fltStride != 1.0:

                    class Down(torch.nn.Module):
                        def __init__(self, fltScale):
                            super().__init__()

                            self.fltScale = fltScale

                        # end

                        def forward(self, tenIn: torch.Tensor) -> torch.Tensor:
                            return torch.nn.functional.interpolate(
                                input=tenIn,
                                scale_factor=self.fltScale,
                                mode="bilinear",
                                align_corners=False,
                            )

                        # end

                    # end

                    self.netShortcut = Down(1.0 / fltStride)

                elif intIn != intOut and fltStride != 1.0:

                    class Down(torch.nn.Module):
                        def __init__(self, fltScale):
                            super().__init__()

                            self.fltScale = fltScale

                        # end

                        def forward(self, tenIn: torch.Tensor) -> torch.Tensor:
                            return torch.nn.functional.interpolate(
                                input=tenIn,
                                scale_factor=self.fltScale,
                                mode="bilinear",
                                align_corners=False,
                            )

                        # end

                    # end

                    self.netShortcut = torch.nn.Sequential(
                        Down(1.0 / fltStride),
                        torch.nn.Conv2d(
                            in_channels=intIn,
                            out_channels=intOut,
                            kernel_size=1,
                            stride=1,
                            padding=0,
                            bias="nobias" not in self.strType.split("+"),
                        ),
                    )

                # end

            elif strPart.startswith("...") == True:
                pass

            # end
        # end

        assert len(intChans) == 1

    # end

    def forward(self, tenIn: torch.Tensor) -> torch.Tensor:
        if self.netEvenize is not None:
            tenIn = self.netEvenize(tenIn)
        # end

        tenOut = self.netMain(tenIn)

        if self.netShortcut is not None:
            tenOut = tenOut + self.netShortcut(tenIn)
        # end

        return tenOut

    # end


# end


##########################################################


class Network(torch.nn.Module):
    def __init__(self):
        super().__init__()

        class Extractor(torch.nn.Module):
            def __init__(self):
                super().__init__()

                self.netOne = Basic(
                    "evenize(replpad)-sconv(2)-prelu(0.25)-conv(3,replpad)-prelu(0.25)-conv(3,replpad)-prelu(0.25)",
                    [3, 32, 32, 32],
                    None,
                )
                self.netTwo = Basic(
                    "evenize(replpad)-sconv(2)-prelu(0.25)-conv(3,replpad)-prelu(0.25)-conv(3,replpad)-prelu(0.25)",
                    [32, 32, 32, 32],
                    None,
                )
                self.netThr = Basic(
                    "evenize(replpad)-sconv(2)-prelu(0.25)-conv(3,replpad)-prelu(0.25)-conv(3,replpad)-prelu(0.25)",
                    [32, 32, 32, 32],
                    None,
                )

            # end

            def forward(self, tenIn):
                tenOne = self.netOne(tenIn)
                tenTwo = self.netTwo(tenOne)
                tenThr = self.netThr(tenTwo)
                tenFou = torch.nn.functional.avg_pool2d(
                    input=tenThr, kernel_size=2, stride=2, count_include_pad=False
                )
                tenFiv = torch.nn.functional.avg_pool2d(
                    input=tenFou, kernel_size=2, stride=2, count_include_pad=False
                )

                return [tenOne, tenTwo, tenThr, tenFou, tenFiv]

            # end

        # end

        class Decoder(torch.nn.Module):
            def __init__(self, intChannels):
                super().__init__()

                self.netCostacti = torch.nn.PReLU(num_parameters=1, init=0.25)
                self.netMain = Basic(
                    "conv(3,replpad)-prelu(0.25)-conv(3,replpad)-prelu(0.25)-conv(3,replpad)-prelu(0.25)-conv(3,replpad)-prelu(0.25)-conv(3,replpad)-prelu(0.25)-conv(3,replpad)",
                    [intChannels, 128, 128, 96, 64, 32, 2],
                    None,
                )

            # end

            def forward(self, tenOne, tenTwo, tenFlow):
                if tenFlow is not None:
                    tenFlow = 2.0 * torch.nn.functional.interpolate(
                        input=tenFlow,
                        scale_factor=2.0,
                        mode="bilinear",
                        align_corners=False,
                    )
                # end

                tenMain = []

                if tenFlow is None:
                    tenMain.append(tenOne)
                    tenMain.append(self.netCostacti(costvol_func.apply(tenOne, tenTwo)))

                elif tenFlow is not None:
                    tenMain.append(tenOne)
                    tenMain.append(
                        self.netCostacti(
                            costvol_func.apply(
                                tenOne, backwarp(tenTwo, tenFlow.detach())
                            )
                        )
                    )
                    tenMain.append(tenFlow)

                # end

                return (tenFlow if tenFlow is not None else 0.0) + self.netMain(
                    torch.cat(tenMain, 1)
                )

            # end

        # end

        self.netExtractor = Extractor()

        self.netFiv = Decoder(32 + 81 + 0)
        self.netFou = Decoder(32 + 81 + 2)
        self.netThr = Decoder(32 + 81 + 2)
        self.netTwo = Decoder(32 + 81 + 2)
        self.netOne = Decoder(32 + 81 + 2)

    # end

    def bidir(self, tenOne, tenTwo):
        tenOne, tenTwo = list(
            zip(
                *[
                    torch.split(tenFeat, [tenOne.shape[0], tenTwo.shape[0]], 0)
                    for tenFeat in self.netExtractor(torch.cat([tenOne, tenTwo], 0))
                ]
            )
        )

        tenFwd = None
        tenFwd = self.netFiv(tenOne[-1], tenTwo[-1], tenFwd)
        tenFwd = self.netFou(tenOne[-2], tenTwo[-2], tenFwd)
        tenFwd = self.netThr(tenOne[-3], tenTwo[-3], tenFwd)
        tenFwd = self.netTwo(tenOne[-4], tenTwo[-4], tenFwd)
        tenFwd = self.netOne(tenOne[-5], tenTwo[-5], tenFwd)

        tenBwd = None
        tenBwd = self.netFiv(tenTwo[-1], tenOne[-1], tenBwd)
        tenBwd = self.netFou(tenTwo[-2], tenOne[-2], tenBwd)
        tenBwd = self.netThr(tenTwo[-3], tenOne[-3], tenBwd)
        tenBwd = self.netTwo(tenTwo[-4], tenOne[-4], tenBwd)
        tenBwd = self.netOne(tenTwo[-5], tenOne[-5], tenBwd)

        return tenFwd, tenBwd

    # end


# end


##########################################################


objCudacache = {}


def cuda_int32(intIn: int):
    return cupy.int32(intIn)


# end


def cuda_float32(fltIn: float):
    return cupy.float32(fltIn)


# end


def cuda_kernel(strFunction: str, strKernel: str, objVariables: typing.Dict):
    if "device" not in objCudacache:
        objCudacache["device"] = torch.cuda.get_device_name()
    # end

    strKey = strFunction

    for strVariable in objVariables:
        objValue = objVariables[strVariable]

        strKey += strVariable

        if objValue is None:
            continue

        elif type(objValue) == int:
            strKey += str(objValue)

        elif type(objValue) == float:
            strKey += str(objValue)

        elif type(objValue) == bool:
            strKey += str(objValue)

        elif type(objValue) == str:
            strKey += objValue

        elif type(objValue) == torch.Tensor:
            strKey += str(objValue.dtype)
            strKey += str(objValue.shape)
            strKey += str(objValue.stride())

        elif True:
            print(strVariable, type(objValue))
            assert False

        # end
    # end

    strKey += objCudacache["device"]

    if strKey not in objCudacache:
        for strVariable in objVariables:
            objValue = objVariables[strVariable]

            if objValue is None:
                continue

            elif type(objValue) == int:
                strKernel = strKernel.replace("{{" + strVariable + "}}", str(objValue))

            elif type(objValue) == float:
                strKernel = strKernel.replace("{{" + strVariable + "}}", str(objValue))

            elif type(objValue) == bool:
                strKernel = strKernel.replace("{{" + strVariable + "}}", str(objValue))

            elif type(objValue) == str:
                strKernel = strKernel.replace("{{" + strVariable + "}}", objValue)

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.uint8:
                strKernel = strKernel.replace("{{type}}", "unsigned char")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.float16:
                strKernel = strKernel.replace("{{type}}", "half")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.float32:
                strKernel = strKernel.replace("{{type}}", "float")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.float64:
                strKernel = strKernel.replace("{{type}}", "double")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.int32:
                strKernel = strKernel.replace("{{type}}", "int")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.int64:
                strKernel = strKernel.replace("{{type}}", "long")

            elif type(objValue) == torch.Tensor:
                print(strVariable, objValue.dtype)
                assert False

            elif True:
                print(strVariable, type(objValue))
                assert False

            # end
        # end

        while True:
            objMatch = re.search("(SIZE_)([0-4])(\()([^\)]*)(\))", strKernel)

            if objMatch is None:
                break
            # end

            intArg = int(objMatch.group(2))

            strTensor = objMatch.group(4)
            intSizes = objVariables[strTensor].size()

            strKernel = strKernel.replace(
                objMatch.group(),
                str(
                    intSizes[intArg]
                    if torch.is_tensor(intSizes[intArg]) == False
                    else intSizes[intArg].item()
                ),
            )
        # end

        while True:
            objMatch = re.search("(OFFSET_)([0-4])(\()", strKernel)

            if objMatch is None:
                break
            # end

            intStart = objMatch.span()[1]
            intStop = objMatch.span()[1]
            intParentheses = 1

            while True:
                intParentheses += 1 if strKernel[intStop] == "(" else 0
                intParentheses -= 1 if strKernel[intStop] == ")" else 0

                if intParentheses == 0:
                    break
                # end

                intStop += 1
            # end

            intArgs = int(objMatch.group(2))
            strArgs = strKernel[intStart:intStop].split(",")

            assert intArgs == len(strArgs) - 1

            strTensor = strArgs[0]
            intStrides = objVariables[strTensor].stride()

            strIndex = []

            for intArg in range(intArgs):
                strIndex.append(
                    "(("
                    + strArgs[intArg + 1].replace("{", "(").replace("}", ")").strip()
                    + ")*"
                    + str(
                        intStrides[intArg]
                        if torch.is_tensor(intStrides[intArg]) == False
                        else intStrides[intArg].item()
                    )
                    + ")"
                )
            # end

            strKernel = strKernel.replace(
                "OFFSET_" + str(intArgs) + "(" + strKernel[intStart:intStop] + ")",
                "(" + str.join("+", strIndex) + ")",
            )
        # end

        while True:
            objMatch = re.search("(VALUE_)([0-4])(\()", strKernel)

            if objMatch is None:
                break
            # end

            intStart = objMatch.span()[1]
            intStop = objMatch.span()[1]
            intParentheses = 1

            while True:
                intParentheses += 1 if strKernel[intStop] == "(" else 0
                intParentheses -= 1 if strKernel[intStop] == ")" else 0

                if intParentheses == 0:
                    break
                # end

                intStop += 1
            # end

            intArgs = int(objMatch.group(2))
            strArgs = strKernel[intStart:intStop].split(",")

            assert intArgs == len(strArgs) - 1

            strTensor = strArgs[0]
            intStrides = objVariables[strTensor].stride()

            strIndex = []

            for intArg in range(intArgs):
                strIndex.append(
                    "(("
                    + strArgs[intArg + 1].replace("{", "(").replace("}", ")").strip()
                    + ")*"
                    + str(
                        intStrides[intArg]
                        if torch.is_tensor(intStrides[intArg]) == False
                        else intStrides[intArg].item()
                    )
                    + ")"
                )
            # end

            strKernel = strKernel.replace(
                "VALUE_" + str(intArgs) + "(" + strKernel[intStart:intStop] + ")",
                strTensor + "[" + str.join("+", strIndex) + "]",
            )
        # end

        objCudacache[strKey] = {"strFunction": strFunction, "strKernel": strKernel}
    # end

    return strKey


# end


@cupy.memoize(for_each_device=True)
def cuda_launch(strKey: str):
    if "CUDA_HOME" not in os.environ:
        os.environ["CUDA_HOME"] = "/usr/local/cuda/"
    # end

    return cupy.cuda.compile_with_cache(
        objCudacache[strKey]["strKernel"],
        tuple(
            [
                "-I " + os.environ["CUDA_HOME"],
                "-I " + os.environ["CUDA_HOME"] + "/include",
            ]
        ),
    ).get_function(objCudacache[strKey]["strFunction"])


# end


##########################################################


def softsplat(
    tenIn: torch.Tensor, tenFlow: torch.Tensor, tenMetric: torch.Tensor, strMode: str
):
    assert strMode.split("-")[0] in ["sum", "avg", "linear", "soft"]

    if strMode == "sum":
        assert tenMetric is None
    if strMode == "avg":
        assert tenMetric is None
    if strMode.split("-")[0] == "linear":
        assert tenMetric is not None
    if strMode.split("-")[0] == "soft":
        assert tenMetric is not None

    if strMode == "avg":
        tenIn = torch.cat(
            [
                tenIn,
                tenIn.new_ones([tenIn.shape[0], 1, tenIn.shape[2], tenIn.shape[3]]),
            ],
            1,
        )

    elif strMode.split("-")[0] == "linear":
        tenIn = torch.cat([tenIn * tenMetric, tenMetric], 1)

    elif strMode.split("-")[0] == "soft":
        tenIn = torch.cat([tenIn * tenMetric.exp(), tenMetric.exp()], 1)

    # end

    tenOut = softsplat_func.apply(tenIn, tenFlow)

    if strMode.split("-")[0] in ["avg", "linear", "soft"]:
        tenNormalize = tenOut[:, -1:, :, :]

        if len(strMode.split("-")) == 1:
            tenNormalize = tenNormalize + 0.0000001

        elif strMode.split("-")[1] == "addeps":
            tenNormalize = tenNormalize + 0.0000001

        elif strMode.split("-")[1] == "zeroeps":
            tenNormalize[tenNormalize == 0.0] = 1.0

        elif strMode.split("-")[1] == "clipeps":
            tenNormalize = tenNormalize.clip(0.0000001, None)

        # end

        tenOut = tenOut[:, :-1, :, :] / tenNormalize
    # end

    return tenOut


# end


class softsplat_func(torch.autograd.Function):
    @staticmethod
    @torch.cuda.amp.custom_fwd(cast_inputs=torch.float32)
    def forward(self, tenIn, tenFlow):
        tenOut = tenIn.new_zeros(
            [tenIn.shape[0], tenIn.shape[1], tenIn.shape[2], tenIn.shape[3]]
        )

        if tenIn.is_cuda == True:
            cuda_launch(
                cuda_kernel(
                    "softsplat_out",
                    """
                extern "C" __global__ void __launch_bounds__(512) softsplat_out(
                    const int n,
                    const {{type}}* __restrict__ tenIn,
                    const {{type}}* __restrict__ tenFlow,
                    {{type}}* __restrict__ tenOut
                ) { for (int intIndex = (blockIdx.x * blockDim.x) + threadIdx.x; intIndex < n; intIndex += blockDim.x * gridDim.x) {
                    const int intN = ( intIndex / SIZE_3(tenOut) / SIZE_2(tenOut) / SIZE_1(tenOut) ) % SIZE_0(tenOut);
                    const int intC = ( intIndex / SIZE_3(tenOut) / SIZE_2(tenOut)                  ) % SIZE_1(tenOut);
                    const int intY = ( intIndex / SIZE_3(tenOut)                                   ) % SIZE_2(tenOut);
                    const int intX = ( intIndex                                                    ) % SIZE_3(tenOut);

                    assert(SIZE_1(tenFlow) == 2);

                    {{type}} fltX = ({{type}}) (intX) + VALUE_4(tenFlow, intN, 0, intY, intX);
                    {{type}} fltY = ({{type}}) (intY) + VALUE_4(tenFlow, intN, 1, intY, intX);

                    if (isfinite(fltX) == false) { return; }
                    if (isfinite(fltY) == false) { return; }

                    {{type}} fltIn = VALUE_4(tenIn, intN, intC, intY, intX);

                    int intNorthwestX = (int) (floor(fltX));
                    int intNorthwestY = (int) (floor(fltY));
                    int intNortheastX = intNorthwestX + 1;
                    int intNortheastY = intNorthwestY;
                    int intSouthwestX = intNorthwestX;
                    int intSouthwestY = intNorthwestY + 1;
                    int intSoutheastX = intNorthwestX + 1;
                    int intSoutheastY = intNorthwestY + 1;

                    {{type}} fltNorthwest = (({{type}}) (intSoutheastX) - fltX) * (({{type}}) (intSoutheastY) - fltY);
                    {{type}} fltNortheast = (fltX - ({{type}}) (intSouthwestX)) * (({{type}}) (intSouthwestY) - fltY);
                    {{type}} fltSouthwest = (({{type}}) (intNortheastX) - fltX) * (fltY - ({{type}}) (intNortheastY));
                    {{type}} fltSoutheast = (fltX - ({{type}}) (intNorthwestX)) * (fltY - ({{type}}) (intNorthwestY));

                    if ((intNorthwestX >= 0) && (intNorthwestX < SIZE_3(tenOut)) && (intNorthwestY >= 0) && (intNorthwestY < SIZE_2(tenOut))) {
                        atomicAdd(&tenOut[OFFSET_4(tenOut, intN, intC, intNorthwestY, intNorthwestX)], fltIn * fltNorthwest);
                    }

                    if ((intNortheastX >= 0) && (intNortheastX < SIZE_3(tenOut)) && (intNortheastY >= 0) && (intNortheastY < SIZE_2(tenOut))) {
                        atomicAdd(&tenOut[OFFSET_4(tenOut, intN, intC, intNortheastY, intNortheastX)], fltIn * fltNortheast);
                    }

                    if ((intSouthwestX >= 0) && (intSouthwestX < SIZE_3(tenOut)) && (intSouthwestY >= 0) && (intSouthwestY < SIZE_2(tenOut))) {
                        atomicAdd(&tenOut[OFFSET_4(tenOut, intN, intC, intSouthwestY, intSouthwestX)], fltIn * fltSouthwest);
                    }

                    if ((intSoutheastX >= 0) && (intSoutheastX < SIZE_3(tenOut)) && (intSoutheastY >= 0) && (intSoutheastY < SIZE_2(tenOut))) {
                        atomicAdd(&tenOut[OFFSET_4(tenOut, intN, intC, intSoutheastY, intSoutheastX)], fltIn * fltSoutheast);
                    }
                } }
            """,
                    {"tenIn": tenIn, "tenFlow": tenFlow, "tenOut": tenOut},
                )
            )(
                grid=tuple([int((tenOut.nelement() + 512 - 1) / 512), 1, 1]),
                block=tuple([512, 1, 1]),
                args=[
                    cuda_int32(tenOut.nelement()),
                    tenIn.data_ptr(),
                    tenFlow.data_ptr(),
                    tenOut.data_ptr(),
                ],
                stream=collections.namedtuple("Stream", "ptr")(
                    torch.cuda.current_stream().cuda_stream
                ),
            )

        elif tenIn.is_cuda != True:
            assert False

        # end

        self.save_for_backward(tenIn, tenFlow)

        return tenOut

    # end

    @staticmethod
    @torch.cuda.amp.custom_bwd
    def backward(self, tenOutgrad):
        tenIn, tenFlow = self.saved_tensors

        tenOutgrad = tenOutgrad.contiguous()
        assert tenOutgrad.is_cuda == True

        tenIngrad = (
            tenIn.new_empty(
                [tenIn.shape[0], tenIn.shape[1], tenIn.shape[2], tenIn.shape[3]]
            )
            if self.needs_input_grad[0] == True
            else None
        )
        tenFlowgrad = (
            tenFlow.new_empty(
                [tenFlow.shape[0], tenFlow.shape[1], tenFlow.shape[2], tenFlow.shape[3]]
            )
            if self.needs_input_grad[1] == True
            else None
        )

        if tenIngrad is not None:
            cuda_launch(
                cuda_kernel(
                    "softsplat_ingrad",
                    """
                extern "C" __global__ void __launch_bounds__(512) softsplat_ingrad(
                    const int n,
                    const {{type}}* __restrict__ tenIn,
                    const {{type}}* __restrict__ tenFlow,
                    const {{type}}* __restrict__ tenOutgrad,
                    {{type}}* __restrict__ tenIngrad,
                    {{type}}* __restrict__ tenFlowgrad
                ) { for (int intIndex = (blockIdx.x * blockDim.x) + threadIdx.x; intIndex < n; intIndex += blockDim.x * gridDim.x) {
                    const int intN = ( intIndex / SIZE_3(tenIngrad) / SIZE_2(tenIngrad) / SIZE_1(tenIngrad) ) % SIZE_0(tenIngrad);
                    const int intC = ( intIndex / SIZE_3(tenIngrad) / SIZE_2(tenIngrad)                     ) % SIZE_1(tenIngrad);
                    const int intY = ( intIndex / SIZE_3(tenIngrad)                                         ) % SIZE_2(tenIngrad);
                    const int intX = ( intIndex                                                             ) % SIZE_3(tenIngrad);

                    assert(SIZE_1(tenFlow) == 2);

                    {{type}} fltIngrad = 0.0f;

                    {{type}} fltX = ({{type}}) (intX) + VALUE_4(tenFlow, intN, 0, intY, intX);
                    {{type}} fltY = ({{type}}) (intY) + VALUE_4(tenFlow, intN, 1, intY, intX);

                    if (isfinite(fltX) == false) { return; }
                    if (isfinite(fltY) == false) { return; }

                    int intNorthwestX = (int) (floor(fltX));
                    int intNorthwestY = (int) (floor(fltY));
                    int intNortheastX = intNorthwestX + 1;
                    int intNortheastY = intNorthwestY;
                    int intSouthwestX = intNorthwestX;
                    int intSouthwestY = intNorthwestY + 1;
                    int intSoutheastX = intNorthwestX + 1;
                    int intSoutheastY = intNorthwestY + 1;

                    {{type}} fltNorthwest = (({{type}}) (intSoutheastX) - fltX) * (({{type}}) (intSoutheastY) - fltY);
                    {{type}} fltNortheast = (fltX - ({{type}}) (intSouthwestX)) * (({{type}}) (intSouthwestY) - fltY);
                    {{type}} fltSouthwest = (({{type}}) (intNortheastX) - fltX) * (fltY - ({{type}}) (intNortheastY));
                    {{type}} fltSoutheast = (fltX - ({{type}}) (intNorthwestX)) * (fltY - ({{type}}) (intNorthwestY));

                    if ((intNorthwestX >= 0) && (intNorthwestX < SIZE_3(tenOutgrad)) && (intNorthwestY >= 0) && (intNorthwestY < SIZE_2(tenOutgrad))) {
                        fltIngrad += VALUE_4(tenOutgrad, intN, intC, intNorthwestY, intNorthwestX) * fltNorthwest;
                    }

                    if ((intNortheastX >= 0) && (intNortheastX < SIZE_3(tenOutgrad)) && (intNortheastY >= 0) && (intNortheastY < SIZE_2(tenOutgrad))) {
                        fltIngrad += VALUE_4(tenOutgrad, intN, intC, intNortheastY, intNortheastX) * fltNortheast;
                    }

                    if ((intSouthwestX >= 0) && (intSouthwestX < SIZE_3(tenOutgrad)) && (intSouthwestY >= 0) && (intSouthwestY < SIZE_2(tenOutgrad))) {
                        fltIngrad += VALUE_4(tenOutgrad, intN, intC, intSouthwestY, intSouthwestX) * fltSouthwest;
                    }

                    if ((intSoutheastX >= 0) && (intSoutheastX < SIZE_3(tenOutgrad)) && (intSoutheastY >= 0) && (intSoutheastY < SIZE_2(tenOutgrad))) {
                        fltIngrad += VALUE_4(tenOutgrad, intN, intC, intSoutheastY, intSoutheastX) * fltSoutheast;
                    }

                    tenIngrad[intIndex] = fltIngrad;
                } }
            """,
                    {
                        "tenIn": tenIn,
                        "tenFlow": tenFlow,
                        "tenOutgrad": tenOutgrad,
                        "tenIngrad": tenIngrad,
                        "tenFlowgrad": tenFlowgrad,
                    },
                )
            )(
                grid=tuple([int((tenIngrad.nelement() + 512 - 1) / 512), 1, 1]),
                block=tuple([512, 1, 1]),
                args=[
                    cuda_int32(tenIngrad.nelement()),
                    tenIn.data_ptr(),
                    tenFlow.data_ptr(),
                    tenOutgrad.data_ptr(),
                    tenIngrad.data_ptr(),
                    None,
                ],
                stream=collections.namedtuple("Stream", "ptr")(
                    torch.cuda.current_stream().cuda_stream
                ),
            )
        # end

        if tenFlowgrad is not None:
            cuda_launch(
                cuda_kernel(
                    "softsplat_flowgrad",
                    """
                extern "C" __global__ void __launch_bounds__(512) softsplat_flowgrad(
                    const int n,
                    const {{type}}* __restrict__ tenIn,
                    const {{type}}* __restrict__ tenFlow,
                    const {{type}}* __restrict__ tenOutgrad,
                    {{type}}* __restrict__ tenIngrad,
                    {{type}}* __restrict__ tenFlowgrad
                ) { for (int intIndex = (blockIdx.x * blockDim.x) + threadIdx.x; intIndex < n; intIndex += blockDim.x * gridDim.x) {
                    const int intN = ( intIndex / SIZE_3(tenFlowgrad) / SIZE_2(tenFlowgrad) / SIZE_1(tenFlowgrad) ) % SIZE_0(tenFlowgrad);
                    const int intC = ( intIndex / SIZE_3(tenFlowgrad) / SIZE_2(tenFlowgrad)                       ) % SIZE_1(tenFlowgrad);
                    const int intY = ( intIndex / SIZE_3(tenFlowgrad)                                             ) % SIZE_2(tenFlowgrad);
                    const int intX = ( intIndex                                                                   ) % SIZE_3(tenFlowgrad);

                    assert(SIZE_1(tenFlow) == 2);

                    {{type}} fltFlowgrad = 0.0f;

                    {{type}} fltX = ({{type}}) (intX) + VALUE_4(tenFlow, intN, 0, intY, intX);
                    {{type}} fltY = ({{type}}) (intY) + VALUE_4(tenFlow, intN, 1, intY, intX);

                    if (isfinite(fltX) == false) { return; }
                    if (isfinite(fltY) == false) { return; }

                    int intNorthwestX = (int) (floor(fltX));
                    int intNorthwestY = (int) (floor(fltY));
                    int intNortheastX = intNorthwestX + 1;
                    int intNortheastY = intNorthwestY;
                    int intSouthwestX = intNorthwestX;
                    int intSouthwestY = intNorthwestY + 1;
                    int intSoutheastX = intNorthwestX + 1;
                    int intSoutheastY = intNorthwestY + 1;

                    {{type}} fltNorthwest = 0.0f;
                    {{type}} fltNortheast = 0.0f;
                    {{type}} fltSouthwest = 0.0f;
                    {{type}} fltSoutheast = 0.0f;

                    if (intC == 0) {
                        fltNorthwest = (({{type}}) (-1.0f)) * (({{type}}) (intSoutheastY) - fltY);
                        fltNortheast = (({{type}}) (+1.0f)) * (({{type}}) (intSouthwestY) - fltY);
                        fltSouthwest = (({{type}}) (-1.0f)) * (fltY - ({{type}}) (intNortheastY));
                        fltSoutheast = (({{type}}) (+1.0f)) * (fltY - ({{type}}) (intNorthwestY));

                    } else if (intC == 1) {
                        fltNorthwest = (({{type}}) (intSoutheastX) - fltX) * (({{type}}) (-1.0f));
                        fltNortheast = (fltX - ({{type}}) (intSouthwestX)) * (({{type}}) (-1.0f));
                        fltSouthwest = (({{type}}) (intNortheastX) - fltX) * (({{type}}) (+1.0f));
                        fltSoutheast = (fltX - ({{type}}) (intNorthwestX)) * (({{type}}) (+1.0f));

                    }

                    for (int intChannel = 0; intChannel < SIZE_1(tenOutgrad); intChannel += 1) {
                        {{type}} fltIn = VALUE_4(tenIn, intN, intChannel, intY, intX);

                        if ((intNorthwestX >= 0) && (intNorthwestX < SIZE_3(tenOutgrad)) && (intNorthwestY >= 0) && (intNorthwestY < SIZE_2(tenOutgrad))) {
                            fltFlowgrad += VALUE_4(tenOutgrad, intN, intChannel, intNorthwestY, intNorthwestX) * fltIn * fltNorthwest;
                        }

                        if ((intNortheastX >= 0) && (intNortheastX < SIZE_3(tenOutgrad)) && (intNortheastY >= 0) && (intNortheastY < SIZE_2(tenOutgrad))) {
                            fltFlowgrad += VALUE_4(tenOutgrad, intN, intChannel, intNortheastY, intNortheastX) * fltIn * fltNortheast;
                        }

                        if ((intSouthwestX >= 0) && (intSouthwestX < SIZE_3(tenOutgrad)) && (intSouthwestY >= 0) && (intSouthwestY < SIZE_2(tenOutgrad))) {
                            fltFlowgrad += VALUE_4(tenOutgrad, intN, intChannel, intSouthwestY, intSouthwestX) * fltIn * fltSouthwest;
                        }

                        if ((intSoutheastX >= 0) && (intSoutheastX < SIZE_3(tenOutgrad)) && (intSoutheastY >= 0) && (intSoutheastY < SIZE_2(tenOutgrad))) {
                            fltFlowgrad += VALUE_4(tenOutgrad, intN, intChannel, intSoutheastY, intSoutheastX) * fltIn * fltSoutheast;
                        }
                    }

                    tenFlowgrad[intIndex] = fltFlowgrad;
                } }
            """,
                    {
                        "tenIn": tenIn,
                        "tenFlow": tenFlow,
                        "tenOutgrad": tenOutgrad,
                        "tenIngrad": tenIngrad,
                        "tenFlowgrad": tenFlowgrad,
                    },
                )
            )(
                grid=tuple([int((tenFlowgrad.nelement() + 512 - 1) / 512), 1, 1]),
                block=tuple([512, 1, 1]),
                args=[
                    cuda_int32(tenFlowgrad.nelement()),
                    tenIn.data_ptr(),
                    tenFlow.data_ptr(),
                    tenOutgrad.data_ptr(),
                    None,
                    tenFlowgrad.data_ptr(),
                ],
                stream=collections.namedtuple("Stream", "ptr")(
                    torch.cuda.current_stream().cuda_stream
                ),
            )
        # end

        return tenIngrad, tenFlowgrad

    # end


# end


##########################################################


def forwarp_mframe_mask(
    tenIn1, tenFlow1, t1, tenIn2, tenFlow2, t2, tenMetric1=None, tenMetric2=None
):
    def one_fdir(tenIn, tenFlow, td, tenMetric):
        tenIn = torch.cat(
            [
                tenIn * td * (tenMetric).clip(-20.0, 20.0).exp(),
                td * (tenMetric).clip(-20.0, 20.0).exp(),
            ],
            1,
        )

        tenOut = softsplat_func.apply(tenIn, tenFlow)

        return tenOut[:, :-1, :, :], tenOut[:, -1:, :, :] + 0.0000001

    flow_num = tenFlow1.shape[0]
    tenOut = 0
    tenNormalize = 0
    for idx in range(flow_num):
        tenOutF, tenNormalizeF = one_fdir(
            tenIn1[idx], tenFlow1[idx], t1[idx], tenMetric1[idx]
        )
        tenOutB, tenNormalizeB = one_fdir(
            tenIn2[idx], tenFlow2[idx], t2[idx], tenMetric2[idx]
        )

        tenOut += tenOutF + tenOutB
        tenNormalize += tenNormalizeF + tenNormalizeB

    return tenOut / tenNormalize, tenNormalize < 0.00001


###################################################################

c = 16


def conv(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
    return torch.nn.Sequential(
        torch.nn.Conv2d(
            in_planes,
            out_planes,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            bias=True,
        ),
        torch.nn.PReLU(out_planes),
    )


def deconv(in_planes, out_planes, kernel_size=4, stride=2, padding=1):
    return torch.nn.Sequential(
        torch.torch.nn.ConvTranspose2d(
            in_channels=in_planes,
            out_channels=out_planes,
            kernel_size=4,
            stride=2,
            padding=1,
            bias=True,
        ),
        torch.nn.PReLU(out_planes),
    )


class Conv2(torch.nn.Module):
    def __init__(self, in_planes, out_planes, stride=2):
        super(Conv2, self).__init__()
        self.conv1 = conv(in_planes, out_planes, 3, stride, 1)
        self.conv2 = conv(out_planes, out_planes, 3, 1, 1)

    def forward(self, x):
        x = self.conv1(x)
        x = self.conv2(x)
        return x


class Conv2n(torch.nn.Module):
    def __init__(self, in_planes, out_planes, stride=2):
        super(Conv2n, self).__init__()
        self.conv1 = conv(in_planes, in_planes, 3, stride, 1)
        self.conv2 = conv(in_planes, in_planes, 3, 1, 1)
        self.conv3 = conv(in_planes, in_planes, 1, 1, 0)
        self.conv4 = conv(in_planes, out_planes, 1, 1, 0)

    def forward(self, x):
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.conv3(x)
        x = self.conv4(x)
        return x


#####################################################


class ImgPyramid(torch.nn.Module):
    def __init__(self):
        super(ImgPyramid, self).__init__()
        self.conv1 = Conv2(3, c)
        self.conv2 = Conv2(c, 2 * c)
        self.conv3 = Conv2(2 * c, 4 * c)
        self.conv4 = Conv2(4 * c, 8 * c)

    def forward(self, x):
        x1 = self.conv1(x)
        x2 = self.conv2(x1)
        x3 = self.conv3(x2)
        x4 = self.conv4(x3)
        return [x1, x2, x3, x4]


class EncDec(torch.nn.Module):
    def __init__(self, branch):
        super(EncDec, self).__init__()
        self.branch = branch

        self.down0 = Conv2(8, 2 * c)
        self.down1 = Conv2(6 * c, 4 * c)
        self.down2 = Conv2(12 * c, 8 * c)
        self.down3 = Conv2(24 * c, 16 * c)

        self.up0 = deconv(48 * c, 8 * c)
        self.up1 = deconv(16 * c, 4 * c)
        self.up2 = deconv(8 * c, 2 * c)
        self.up3 = deconv(4 * c, c)
        self.conv = torch.nn.Conv2d(c, 2 * self.branch, 3, 1, 1)

        self.conv_m = torch.nn.Conv2d(c, 1, 3, 1, 1)

        # For Channel dimennsion
        self.conv_C = torch.nn.Sequential(
            torch.nn.AdaptiveAvgPool2d(1),
            torch.nn.Conv2d(
                16 * c,
                16 * 16 * c,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
                bias=True,
            ),
            torch.nn.Sigmoid(),
        )

        # For Height dimennsion
        self.conv_H = torch.nn.Sequential(
            torch.nn.AdaptiveAvgPool2d((None, 1)),
            torch.nn.Conv2d(
                16 * c, 16, kernel_size=(1, 1), stride=(1, 1), padding=(0, 0), bias=True
            ),
            torch.nn.Sigmoid(),
        )

        # For Width dimennsion
        self.conv_W = torch.nn.Sequential(
            torch.nn.AdaptiveAvgPool2d((1, None)),
            torch.nn.Conv2d(
                16 * c, 16, kernel_size=(1, 1), stride=(1, 1), padding=(0, 0), bias=True
            ),
            torch.nn.Sigmoid(),
        )

        self.sigmoid = torch.nn.Sigmoid()

    def forward(self, flow0, flow1, im0, im1, c0, c1):
        N_, C_, H_, W_ = im0.shape

        wim1 = backwarp(im1, flow0)
        wim0 = backwarp(im0, flow1)
        s0_0 = self.down0(torch.cat((flow0, im0, wim1), 1))
        s1_0 = self.down0(torch.cat((flow1, im1, wim0), 1))

        #########################################################################################
        flow0 = (
            torch.nn.functional.interpolate(
                flow0, scale_factor=0.5, mode="bilinear", align_corners=False
            )
            * 0.5
        )
        flow1 = (
            torch.nn.functional.interpolate(
                flow1, scale_factor=0.5, mode="bilinear", align_corners=False
            )
            * 0.5
        )

        wf0 = backwarp(torch.cat((s0_0, c0[0]), 1), flow1)
        wf1 = backwarp(torch.cat((s1_0, c1[0]), 1), flow0)

        s0_1 = self.down1(torch.cat((s0_0, c0[0], wf1), 1))
        s1_1 = self.down1(torch.cat((s1_0, c1[0], wf0), 1))

        #########################################################################################
        flow0 = (
            torch.nn.functional.interpolate(
                flow0, scale_factor=0.5, mode="bilinear", align_corners=False
            )
            * 0.5
        )
        flow1 = (
            torch.nn.functional.interpolate(
                flow1, scale_factor=0.5, mode="bilinear", align_corners=False
            )
            * 0.5
        )

        wf0 = backwarp(torch.cat((s0_1, c0[1]), 1), flow1)
        wf1 = backwarp(torch.cat((s1_1, c1[1]), 1), flow0)

        s0_2 = self.down2(torch.cat((s0_1, c0[1], wf1), 1))
        s1_2 = self.down2(torch.cat((s1_1, c1[1], wf0), 1))

        #########################################################################################
        flow0 = (
            torch.nn.functional.interpolate(
                flow0, scale_factor=0.5, mode="bilinear", align_corners=False
            )
            * 0.5
        )
        flow1 = (
            torch.nn.functional.interpolate(
                flow1, scale_factor=0.5, mode="bilinear", align_corners=False
            )
            * 0.5
        )

        wf0 = backwarp(torch.cat((s0_2, c0[2]), 1), flow1)
        wf1 = backwarp(torch.cat((s1_2, c1[2]), 1), flow0)

        s0_3 = self.down3(torch.cat((s0_2, c0[2], wf1), 1))
        s1_3 = self.down3(torch.cat((s1_2, c1[2], wf0), 1))

        #########################################################################################

        s0_3_c = self.conv_C(s0_3)
        s0_3_c = s0_3_c.view(N_, 16, -1, 1, 1)

        s0_3_h = self.conv_H(s0_3)
        s0_3_h = s0_3_h.view(N_, 16, 1, -1, 1)

        s0_3_w = self.conv_W(s0_3)
        s0_3_w = s0_3_w.view(N_, 16, 1, 1, -1)

        cube0 = (s0_3_c * s0_3_h * s0_3_w).mean(1)

        s0_3 = s0_3 * cube0

        s1_3_c = self.conv_C(s1_3)
        s1_3_c = s1_3_c.view(N_, 16, -1, 1, 1)

        s1_3_h = self.conv_H(s1_3)
        s1_3_h = s1_3_h.view(N_, 16, 1, -1, 1)

        s1_3_w = self.conv_W(s1_3)
        s1_3_w = s1_3_w.view(N_, 16, 1, 1, -1)

        cube1 = (s1_3_c * s1_3_h * s1_3_w).mean(1)

        s1_3 = s1_3 * cube1

        #########################################################################################
        flow0 = (
            torch.nn.functional.interpolate(
                flow0, scale_factor=0.5, mode="bilinear", align_corners=False
            )
            * 0.5
        )
        flow1 = (
            torch.nn.functional.interpolate(
                flow1, scale_factor=0.5, mode="bilinear", align_corners=False
            )
            * 0.5
        )

        wf0 = backwarp(torch.cat((s0_3, c0[3]), 1), flow1)
        wf1 = backwarp(torch.cat((s1_3, c1[3]), 1), flow0)

        x0 = self.up0(torch.cat((s0_3, c0[3], wf1), 1))
        x1 = self.up0(torch.cat((s1_3, c1[3], wf0), 1))

        x0 = self.up1(torch.cat((s0_2, x0), 1))
        x1 = self.up1(torch.cat((s1_2, x1), 1))

        x0 = self.up2(torch.cat((s0_1, x0), 1))
        x1 = self.up2(torch.cat((s1_1, x1), 1))

        x0 = self.up3(torch.cat((s0_0, x0), 1))
        x1 = self.up3(torch.cat((s1_0, x1), 1))

        m0 = self.sigmoid(self.conv_m(x0)) * 0.8 + 0.1
        m1 = self.sigmoid(self.conv_m(x1)) * 0.8 + 0.1

        x0 = self.conv(x0)
        x1 = self.conv(x1)

        return x0, x1, m0.repeat(1, self.branch, 1, 1), m1.repeat(1, self.branch, 1, 1)


class M2M_PWC(torch.nn.Module):
    def __init__(self, ratio=4):
        super(M2M_PWC, self).__init__()
        self.branch = 4
        self.ratio = ratio

        self.netFlow = Network()

        self.paramAlpha = torch.nn.Parameter(10.0 * torch.ones(1, 1, 1, 1))

        class MotionRefineNet(torch.nn.Module):
            def __init__(self, branch):
                super(MotionRefineNet, self).__init__()
                self.branch = branch
                self.img_pyramid = ImgPyramid()
                self.motion_encdec = EncDec(branch)

            def forward(self, flow0, flow1, im0, im1, ratio):
                flow0 = ratio * torch.nn.functional.interpolate(
                    input=flow0,
                    scale_factor=ratio,
                    mode="bilinear",
                    align_corners=False,
                )
                flow1 = ratio * torch.nn.functional.interpolate(
                    input=flow1,
                    scale_factor=ratio,
                    mode="bilinear",
                    align_corners=False,
                )

                c0 = self.img_pyramid(im0)
                c1 = self.img_pyramid(im1)

                flow_res = self.motion_encdec(flow0, flow1, im0, im1, c0, c1)

                flow0 = flow0.repeat(1, self.branch, 1, 1) + flow_res[0]
                flow1 = flow1.repeat(1, self.branch, 1, 1) + flow_res[1]

                return flow0, flow1, flow_res[2], flow_res[3]

        self.MRN = MotionRefineNet(self.branch)

    def forward(self, im0, im1, fltTimes=[0.5], ratio=None):
        if ratio is None:
            ratio = self.ratio

        intWidth = im0.shape[3] and im1.shape[3]
        intHeight = im0.shape[2] and im1.shape[2]

        intPadr = ((ratio * 16) - (intWidth % (ratio * 16))) % (ratio * 16)
        intPadb = ((ratio * 16) - (intHeight % (ratio * 16))) % (ratio * 16)

        im0 = torch.nn.functional.pad(
            input=im0, pad=[0, intPadr, 0, intPadb], mode="replicate"
        )
        im1 = torch.nn.functional.pad(
            input=im1, pad=[0, intPadr, 0, intPadb], mode="replicate"
        )

        N_, C_, H_, W_ = im0.shape

        outputs = []

        with torch.set_grad_enabled(False):
            tenStats = [im0, im1]
            tenMean_ = sum([tenIn.mean([1, 2, 3], True) for tenIn in tenStats]) / len(
                tenStats
            )
            tenStd_ = (
                sum(
                    [
                        tenIn.std([1, 2, 3], False, True).square()
                        + (tenMean_ - tenIn.mean([1, 2, 3], True)).square()
                        for tenIn in tenStats
                    ]
                )
                / len(tenStats)
            ).sqrt()

            im0_o = (im0 - tenMean_) / (tenStd_ + 0.0000001)
            im1_o = (im1 - tenMean_) / (tenStd_ + 0.0000001)

            im0 = (im0 - tenMean_) / (tenStd_ + 0.0000001)
            im1 = (im1 - tenMean_) / (tenStd_ + 0.0000001)

        im0_ = torch.nn.functional.interpolate(
            input=im0, scale_factor=2.0 / ratio, mode="bilinear", align_corners=False
        )
        im1_ = torch.nn.functional.interpolate(
            input=im1, scale_factor=2.0 / ratio, mode="bilinear", align_corners=False
        )

        tenFwd, tenBwd = self.netFlow.bidir(im0_, im1_)

        tenFwd, tenBwd, WeiMF, WeiMB = self.MRN(tenFwd, tenBwd, im0, im1, ratio)

        for fltTime_ in fltTimes:
            im0 = im0_o.repeat(1, self.branch, 1, 1)
            im1 = im1_o.repeat(1, self.branch, 1, 1)
            tenStd = tenStd_.repeat(1, self.branch, 1, 1)
            tenMean = tenMean_.repeat(1, self.branch, 1, 1)
            fltTime = fltTime_.repeat(1, self.branch, 1, 1)

            tenFwd = tenFwd.reshape(N_, self.branch, 2, H_, W_).view(
                N_ * self.branch, 2, H_, W_
            )
            tenBwd = tenBwd.reshape(N_, self.branch, 2, H_, W_).view(
                N_ * self.branch, 2, H_, W_
            )

            WeiMF = WeiMF.reshape(N_, self.branch, 1, H_, W_).view(
                N_ * self.branch, 1, H_, W_
            )
            WeiMB = WeiMB.reshape(N_, self.branch, 1, H_, W_).view(
                N_ * self.branch, 1, H_, W_
            )

            im0 = im0.reshape(N_, self.branch, 3, H_, W_).view(
                N_ * self.branch, 3, H_, W_
            )
            im1 = im1.reshape(N_, self.branch, 3, H_, W_).view(
                N_ * self.branch, 3, H_, W_
            )

            tenStd = tenStd.reshape(N_, self.branch, 1, 1, 1).view(
                N_ * self.branch, 1, 1, 1
            )
            tenMean = tenMean.reshape(N_, self.branch, 1, 1, 1).view(
                N_ * self.branch, 1, 1, 1
            )
            fltTime = fltTime.reshape(N_, self.branch, 1, 1, 1).view(
                N_ * self.branch, 1, 1, 1
            )

            tenPhotoone = (
                (
                    1.0
                    - (
                        WeiMF
                        * (im0 - backwarp(im1, tenFwd).detach()).abs().mean([1], True)
                    )
                )
                .clip(0.001, None)
                .square()
            )
            tenPhototwo = (
                (
                    1.0
                    - (
                        WeiMB
                        * (im1 - backwarp(im0, tenBwd).detach()).abs().mean([1], True)
                    )
                )
                .clip(0.001, None)
                .square()
            )

            t0 = fltTime
            flow0 = tenFwd * t0
            metric0 = self.paramAlpha * tenPhotoone

            t1 = 1.0 - fltTime
            flow1 = tenBwd * t1
            metric1 = self.paramAlpha * tenPhototwo

            flow0 = flow0.reshape(N_, self.branch, 2, H_, W_).permute(1, 0, 2, 3, 4)
            flow1 = flow1.reshape(N_, self.branch, 2, H_, W_).permute(1, 0, 2, 3, 4)

            metric0 = metric0.reshape(N_, self.branch, 1, H_, W_).permute(1, 0, 2, 3, 4)
            metric1 = metric1.reshape(N_, self.branch, 1, H_, W_).permute(1, 0, 2, 3, 4)

            im0 = im0.reshape(N_, self.branch, 3, H_, W_).permute(1, 0, 2, 3, 4)
            im1 = im1.reshape(N_, self.branch, 3, H_, W_).permute(1, 0, 2, 3, 4)

            t0 = t0.reshape(N_, self.branch, 1, 1, 1).permute(1, 0, 2, 3, 4)
            t1 = t1.reshape(N_, self.branch, 1, 1, 1).permute(1, 0, 2, 3, 4)

            tenOutput, mask = forwarp_mframe_mask(
                im0, flow0, t1, im1, flow1, t0, metric0, metric1
            )

            tenOutput = tenOutput + mask * (t1.mean(0) * im0_o + t0.mean(0) * im1_o)

            outputs.append((tenOutput * (tenStd_ + 0.0000001)) + tenMean_)

        return [output[:, :, :intHeight, :intWidth] for output in outputs]
