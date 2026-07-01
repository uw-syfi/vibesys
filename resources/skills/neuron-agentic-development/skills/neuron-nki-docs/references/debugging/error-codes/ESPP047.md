# NCC_ESPP047

**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

NCC_ESPP047
**Error message**: The compiler found usage of an unsupported 8-bit floating-point data type.

Erroneous code example:


```python
class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = nn.Linear(10, 20)
        self.linear2 = nn.Linear(20, 10)

    def forward(self, x):
        x = self.linear1(x)
        x = torch.relu(x)
        x = self.linear2(x)
        return x

# Unsupported 8-bit floating-point data type being used here
input_tensor = torch.randn(1, 10).to(torch.float8_e4m3fn)
```


To fix this error:


```python
class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = nn.Linear(10, 20)
        self.linear2 = nn.Linear(20, 10)

    def forward(self, x):
        x = self.linear1(x)
        x = torch.relu(x)
        x = self.linear2(x)
        return x

input_tensor = torch.randn(1, 10).to(torch.float8_e4m3fn)
# Convert to a supported type
input_tensor = input_tensor.to(torch.float16)
```


**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`