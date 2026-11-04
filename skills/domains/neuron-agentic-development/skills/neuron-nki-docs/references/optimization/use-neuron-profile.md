# Profile a NKI Kernel

Profile a NKI Kernel
Learn how to profile Neuron Kernel Interface (NKI) kernels using Neuron Explorer to analyze hardware-level performance characteristics on Trainium and Inferentia devices. This comprehensive guide covers two profiling methods: using the `neuron-profile capture` command-line tool and the `&#64;nki.profile` decorator API. You’ll discover how to generate NEFF and NTFF files, identify performance bottlenecks, optimize kernel execution, and leverage the interactive web-based Neuron Profile UI to visualize execution traces with source code integration for efficient NKI kernel development and optimization.

## Install Neuron Explorer

Ensure that you have the latest version of the `aws-neuronx-tools` package installed as Neuron Explorer comes with this package. The `aws-neuronx-tools` package is pre-installed on Neuron DLAMIs.

* For detailed installation instructions, see: [How to Get Started with Neuron Explorer](../../tools/neuron-explorer/get-started.md#new-neuron-profiler-setup).

## Profile a NKI Kernel

Profiling NKI (Neuron Kernel Interface) kernels helps you understand hardware level performance characteristics of your kernels running on AWS Trainium and Inferentia devices. When you write or optimize custom NKI kernels, profiling allows you to:

* **Identify bottlenecks**: Determine if your kernel is compute-bound, memory-bound, or limited by data movement.

* **Optimize performance**: Analyze kernel-level execution time, investigate compute engine utilization, look for opportunities to implement operator fusion to fine-tune performance.

* **Compare implementations**: Benchmark different kernel implementations or configurations to pick the most efficient kernel.

You can profile NKI kernels using several approaches. In this guide, you’ll learn two primary methods for profiling NKI kernels.

### How to profile using neuron-profile capture

To profile an NKI kernel using neuron-profile capture, follow these three steps:

* Set the environment variable `NEURON_FRAMEWORK_DEBUG=1` to instruct the compiler to save the NEFF (Neuron Executable File Format) file.

* Execute the NKI kernel to generate the NEFF file.

* Run `neuron-profile capture` to create an Neuron Trace File Format (NTFF) file for performance analysis.

Each of these steps is explained in detail below.

#### Step 1: Set Environment Variables

We will profile a 3-layer MLP model that fuses matrix multiplications with ReLU activation functions and uses a NKI matrix multiplication kernel. The rest of this tutorial will use a performance profile generated from this example. Here is the implementation of `mlp_with_mm_kernel.py`. Save this file before moving on to the next step:


```python
"""
Example 3-layer MLP with matrix multiplication kernel to demonstrate Neuron Profile.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import nki
import nki.isa as nisa
import nki.language as nl
import os

os.environ["NEURON_FRAMEWORK_DEBUG"] = "1"
os.environ["XLA_IR_DEBUG"] = "1"
os.environ["XLA_HLO_DEBUG"] = "1"
os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"


@nki.jit
def nki_matmul_fully_optimized_(
    lhsT,
    rhs,
    # Meta-parameters
    TILES_IN_BLOCK_M=16,
    TILES_IN_BLOCK_N=2,
    TILES_IN_BLOCK_K=8,
):
  """NKI kernel to compute a large matrix multiplication efficiently by
     blocking all dimensions and doing layout optimization.

  Args:
      lhsT: an input tensor of shape [K,M], where K is a multiple of 128 *
        TILES_IN_BLOCK_K and M is a multiple of 128 * TILES_IN_BLOCK_M.  It is the
        left-hand-side argument of the matrix multiplication, delivered transposed
        for optimal performance.
      rhs: an input tensor of shape [K,N],  where K is a multiple of 128 *
        TILES_IN_BLOCK_K and N is a multiple of 512 * TILES_IN_BLOCK_N.  It is
        the right-hand-side argument of the matrix multiplication.
      TILES_IN_BLOCK_*: meta parameters to control blocking dimensions
  Returns:
      result: the resulting output tensor of shape [M,N]
  """

  K, M = lhsT.shape
  K_, N = rhs.shape
  assert K == K_, "lhsT and rhs must have the same contraction dimension"
  result = nl.ndarray((M, N), dtype=lhsT.dtype, buffer=nl.shared_hbm)

  TILE_M = nl.tile_size.gemm_stationary_fmax  # 128
  TILE_K = nl.tile_size.pmax  # 128
  TILE_N = nl.tile_size.gemm_moving_fmax  # 512

  BLOCK_M = TILE_M * TILES_IN_BLOCK_M
  BLOCK_N = TILE_N * TILES_IN_BLOCK_N
  BLOCK_K = TILE_K * TILES_IN_BLOCK_K

  # the size has to be multiple of block size
  assert M % BLOCK_M == 0
  assert N % BLOCK_N == 0
  assert K % BLOCK_K == 0

  NUM_BLOCK_M = M // BLOCK_M
  NUM_BLOCK_N = N // BLOCK_N
  NUM_BLOCK_K = K // BLOCK_K

  # Blocking N dimension (the RHS free dimension)
  for n in range(NUM_BLOCK_N):
    result_tiles = nl.zeros((NUM_BLOCK_M, TILES_IN_BLOCK_M, TILES_IN_BLOCK_N,
                             nl.par_dim(TILE_M), TILE_N),
                            dtype=lhsT.dtype,
                            buffer=nl.sbuf)

    # Blocking K dimension (the contraction dimension)
    for k in range(NUM_BLOCK_K):
      # Loading tiles from rhs
      # setting the load tile to `TILE_K x BLOCK_SIZE_N` to optimize DMA performance
      i_rhs = nl.mgrid[0:TILE_K, 0:BLOCK_N]
      rhs_tiles = nl.ndarray((TILES_IN_BLOCK_K, nl.par_dim(TILE_K), BLOCK_N),
                             dtype=rhs.dtype,
                             buffer=nl.sbuf)

      for bk_r in range(TILES_IN_BLOCK_K):
        nisa.dma_copy(dst=rhs_tiles[bk_r, i_rhs.p, i_rhs.x],
            src=rhs[(TILES_IN_BLOCK_K * k + bk_r) * TILE_K + i_rhs.p,
                BLOCK_N * n + i_rhs.x])

      # Blocking M dimension (the LHS free dimension)
      for m in range(NUM_BLOCK_M):
        # Loading tiles from lhsT
        i_lhsT = nl.mgrid[0:TILE_K, 0:BLOCK_M]
        lhsT_tiles = nl.ndarray((TILES_IN_BLOCK_K, nl.par_dim(TILE_K), BLOCK_M),
                                dtype=lhsT.dtype,
                                buffer=nl.sbuf)
        for bk_l in range(TILES_IN_BLOCK_K):
          nisa.dma_copy(dst=lhsT_tiles[bk_l, i_lhsT.p, i_lhsT.x],
              src=lhsT[(TILES_IN_BLOCK_K * k + bk_l) * TILE_K + i_lhsT.p,
                   BLOCK_M * m + i_lhsT.x])

        # Do matmul with all tiles in the blocks
        i_lhsT_mm = nl.mgrid[0:TILE_K, 0:TILE_M]
        i_rhs_mm = nl.mgrid[0:TILE_K, 0:TILE_N]
        i_res_mm = nl.mgrid[0:TILE_M, 0:TILE_N]
        for bn in range(TILES_IN_BLOCK_N):
          for bm in range(TILES_IN_BLOCK_M):
            res_tile = nl.zeros((TILE_M, TILE_N), dtype=nl.float32, buffer=nl.psum)

            for bk in range(TILES_IN_BLOCK_K):
              res_tile[...] += nisa.nc_matmul(
                  lhsT_tiles[bk, i_lhsT_mm.p, bm * TILE_M + i_lhsT_mm.x],
                  rhs_tiles[bk, i_rhs_mm.p, bn * TILE_N + i_rhs_mm.x])

            # Accumulate on corresponding SBUF tile
            result_tiles[m, bm, bn, i_res_mm.p,
                         i_res_mm.x] += res_tile[i_res_mm.p, i_res_mm.x]

    # Copying the result from SBUF to HBM
    for m in range(NUM_BLOCK_M):
      for bm in range(TILES_IN_BLOCK_M):
        i_res = nl.mgrid[0:TILE_M, 0:TILE_N]
        i_res_packed = nl.mgrid[0:TILE_M, 0:BLOCK_N]
        result_packed = nl.ndarray((TILE_M, BLOCK_N),
                                   dtype=result_tiles.dtype,
                                   buffer=nl.sbuf)

        # coalesce result tiles for better DMA performance
        for bn in range(TILES_IN_BLOCK_N):
          result_packed[i_res.p,
                        bn * TILE_N + i_res.x] = nl.copy(result_tiles[m, bm, bn,
                                                                      i_res.p,
                                                                      i_res.x])
        nl.store(result[(TILES_IN_BLOCK_M * m + bm) * TILE_M + i_res_packed.p,
                        BLOCK_N * n + i_res_packed.x],
                 value=result_packed[i_res_packed.p, i_res_packed.x])

  return result


class NKILinear(nn.Module):
    def __init__(self, in_features, out_features):
        super(NKILinear, self).__init__()
        self.weight = nn.Parameter(torch.randn(out_features, in_features))
        self.bias = nn.Parameter(torch.randn(out_features))

    def forward(self, x):
        weight_T = self.weight.t()
        x_T = x.t()
        output = nki_matmul_fully_optimized_(x_T, weight_T)
        return output + self.bias


class MLP(nn.Module):
    def __init__(self):
        super(MLP, self).__init__()
        self.fc1 = NKILinear(2048, 2048)
        self.fc2 = NKILinear(2048, 1024)
        self.fc3 = NKILinear(1024, 1024)

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return F.log_softmax(x, dim=1)


def main():
    from torch_xla.core import xla_model as xm

    torch.manual_seed(0)
    device = xm.xla_device()

    model = MLP().to(device)
    train_x = torch.randn(2048, 2048).to(device)

    output = model(train_x)

    print(f"Output tensor: {output}")

    xm.mark_step()


if __name__ == "__main__":
    main()
```


As you can see, at the very top we have added the following flags:


```python
os.environ["NEURON_FRAMEWORK_DEBUG"] = "1"
os.environ["XLA_IR_DEBUG"] = "1"
os.environ["XLA_HLO_DEBUG"] = "1"
```


The `NEURON_FRAMEWORK_DEBUG` environment variable enables Neuron debug output. This will trigger the Neuron compiler to save the Neuron Executable File Format (NEFF) artifact to the current directory after compilation of your NKI kernel. The NEFF contains all hardware instructions required to execute your NKI kernel on a NeuronDevice, as well as metadata and debug info needed for profiling. To enable source code linking to framework code (ex. PyTorch) set the environment variables `XLA_IR_DEBUG=1` and `XLA_HLO_DEBUG=1`.

#### Step 2: Compile Your NKI Kernel

Compile your NKI kernel to create a NEFF in your current directory:


```python
$ python3 mlp_with_mm_kernel.py
```


> **Note**
>
> Note
> 
> 
> Find your NEFF file, which will be named something like `MODULE_SyncTensorsGraph.81_690876920003119736.neff`.

#### Step 3: Profile the Generated NEFF

The last step is profiling the generated NEFF. This step executes the NEFF on the NeuronDevice and records a raw execution trace into a NTFF artifact:


```python
$ neuron-explorer capture -n <path_to_neff> -s profile.ntff --profile-nth-exec=2 --enable-dge-notifs
```


This will save your NTFF profile to `profile_exec_2.ntff`.

important:


```python
The ``--profile-nth-exec=2`` option will profile your NEFF twice on the NeuronDevice and output a NTFF profile for the second iteration. This is recommended to avoid one-time warmup delays which can be seen in the first iteration of execution.

The ``--enable-dge-notifs`` option enables the capture of DGE DMA events but has known issues where it may overflow the status notification queue and cause execution timeouts when there are many DGE instructions.
```


## View the Neuron Explorer UI

This section assumes you’ve completed the previous step and have already generated both the NEFF and NTFF files, and downloaded them on your local machine.

Neuron Explorer includes an interactive, web-based UI for exploring execution traces in detail. In this section, we’ll open the Neuron Explorer UI to examine NKI-specific profiling information. These details can be found in multiple areas of the interface — including instruction hover tooltips, instruction click panels, search results, and box select results.

To view the Neuron Profile Web UI, execute the view command to start Web UI, replacing `<workspace>` with a path to a folder to store your profiling artifacts:


```python
$ neuron-explorer view --data-path ./<workspace>
```


`<workspace>` is a path that neuron profile will use for storing and managing profiles.

The above command should print a URL that you can click to open the web UI:


```python
View a list of profiles at http://localhost:3001/
```


### Port Forwarding for Remote Instances

If `neuron-profile view` is run on a remote instance, you may need to use port forwarding to access the web UI. By default, neuron-profile creates a web server on port 3001 and the API server on port 3002. To enable connection to your browser in you local computer, we will need to establish an ssh tunnel to both of the ports.

For example:


```python
ssh -L 3001:localhost:3001 -L 3002:localhost:3002 <user>@<ip> -fN
```


If you created an EC2 instance with `pem` credentials, include it in the `ssh` tunnel below:


```python
ssh -i ~/my-ec2.pem -L 3001:localhost:3001 -L 3002:localhost:3002 ubuntu@[PUBLIC_IP_ADDRESS] -fN
```


### Using the Profile UI

* Once the ssh tunnel is setup, you can now open a browser and navigate to [http://localhost:3001](http://localhost:3001).


> **Figure: nki profiler 1**
>
> A screenshot of the Neuron Profiler web interface showing the Profile Manager page with an empty profiles list and options to upload, search, and manage profiling data.
>
> This screenshot displays the Neuron Profiler web application interface, which is used for analyzing performance profiles from NeuronCore hardware. The interface has a dark header bar and a light-gray main content area.
>
> The top header bar shows "Neuron Profiler" as the application title on the left, and a user menu showing "myself" with a dropdown on the right.
>
> The left sidebar navigation panel (dark background) contains:
> - "Profile" section header with a collapse arrow
> - "Profile Manager" (highlighted/selected)
> - "Profile" link
> - "Summary" link
>
> The main content area displays the "Profile Manager" page:
> - Title: "Profile Manager"
> - Subtitle: "Times are displayed in America/Toronto time"
> - Blue "Upload Profile" button in the top right corner
>
> Below the title are four navigation tabs:
> - "User uploaded" (currently selected, underlined)
> - "User favorite"
> - "Search Profile"
> - "View History"
>
> The profiles section shows:
> - Header: "Profiles (0)" with subtitle "My Uploaded Profiles"
> - Pagination controls showing "1" with navigation arrows
> - A settings gear icon
>
> A data table with column headers:
> - Status (with filter dropdown)
> - Profile Name (with filter dropdown)
> - Complete P... (truncated, with filter dropdown)
> - Upl... (truncated, with filter dropdown)
> - Upload Time (with filter dropdown, selected/highlighted)
> - Last View (with filter dropdown)
> - Actions
>
> The table body shows an empty state with a search/magnifying glass icon and the message "No profiles found - No profiles available for the selected type."
>
> **Key Elements:**
> - **Neuron Profiler**: Application title in header
> - **Profile Manager**: Main page for managing uploaded profiles
> - **Upload Profile button**: Blue button to upload new profile data
> - **Tab navigation**: User uploaded, User favorite, Search Profile, View History
> - **Empty state**: No profiles currently available
> - **Column filters**: Sortable/filterable columns for profile management
> - **User menu**: "myself" dropdown for user account options


* Click on the button “Upload Profile” to upload NEFF and NTFF files, and give a meaningful name to your profile. Selecting a source code folder for code linking is optional.


> **Figure: nki profiler 2**
>
> A screenshot of the Neuron Profiler "Upload Profile" dialog showing fields for uploading NEFF files, NTFF files, and source code for performance analysis.
>
> This screenshot shows a modal dialog box overlaying the Profile Manager page in the Neuron Profiler web application. The dialog is used to upload profiling artifacts for analysis.
>
> The dialog has a dark header with "Upload Profile" title and an X close button in the top right corner.
>
> The dialog contains several input sections:
>
> **Profile Name Section:**
> - Text input field containing "mlp_with_mm_kernel" as the profile name
>
> **NEFF File Section (Required):**
> - Header: "NEFF File" with "Required" label
> - Upload area with upload icon and text "Drop NEFF file" / "Drag .neff file or browse"
> - "Browse Files" button
> - "Selected File" subsection showing:
>   - File: "graph.neff" (261 KB)
>   - Blue "NEFF" badge indicator
>   - X button to remove the file
>
> **NTFF File Section (Required):**
> - Header: "NTFF File" with "Required" label  
> - Upload area with upload icon and text "Drop NTFF file" / "Drag .ntff file or browse"
> - "Browse Files" button
> - "Selected File" subsection showing:
>   - File: "profile_exec_2.ntff" (3.26 MB)
>   - Blue "NTFF" badge indicator
>   - X button to remove the file
>
> **Source Code Section:**
> - Header: "Source Code"
> - Upload area with upload icon and text "Drop source code files or folders" / "Drag files/folders or browse"
> - Two buttons: "Browse Files" and "Browse Folders"
> - "Selected File" subsection showing:
>   - File: "mlp_with_mm_kernel.py" (6.5 KB)
>   - Green "test/x-python-script" badge
>   - X button to remove the file
>
> **Options Section:**
> - Checkbox (unchecked): "Force upload (overwrite existing profile with same NEFF and NTFF)"
>
> **Action Buttons:**
> - "Cancel" button (gray)
> - "Upload" button (blue)
>
> **Key Elements:**
> - **Profile Name**: Text field for naming the profile
> - **NEFF File upload**: Required compiled model file (graph.neff selected)
> - **NTFF File upload**: Required profiling trace file (profile_exec_2.ntff selected)
> - **Source Code upload**: Optional Python source files (mlp_with_mm_kernel.py selected)
> - **Force upload option**: Checkbox to overwrite existing profiles
> - **File badges**: NEFF, NTFF, and script type indicators
> - **Cancel/Upload buttons**: Dialog action buttons


* After the files are uploaded and processed, you will be able to open the profile from the list.


> **Figure: nki profiler 3**
>
> A screenshot of the Neuron Profiler Profile Manager showing a successfully processed profile named "mlp_with_mm_kernel" in the uploaded profiles list.
>
> This screenshot displays the Neuron Profiler web interface with the Profile Manager page showing one uploaded and processed profile. The interface uses a dark theme with a light content area.
>
> The header shows "Neuron Profiler" on the left and "myself" user menu on the right with a dropdown arrow.
>
> The left sidebar navigation shows:
> - "Profile" section with collapse arrow
> - "Profile Manager" (highlighted/selected)
> - "Profile" link
> - "Summary" link
>
> The main content area shows:
> - Title: "Profile Manager"
> - Subtitle: "Times are displayed in America/Toronto time"
> - Blue "Upload Profile" button in the top right
>
> Tab navigation shows four tabs:
> - "User uploaded" (selected, underlined)
> - "User favorite"
> - "Search Profile"
> - "View History"
>
> The Profiles section displays:
> - Header: "Profiles (1)" indicating one profile
> - Subtitle: "My Uploaded Profiles"
> - Search box with placeholder "Filter profiles..."
> - Refresh icon on the right
> - Pagination showing "1" with navigation arrows
> - Settings gear icon
>
> The data table shows one row with columns:
> - **Status**: Green checkmark icon with "PROCESSED" label
> - **Profile Name**: "mlp_with_mm_kernel" (clickable link)
> - **Complete P...**: (truncated column)
> - **Upl...**: "11/12/2025, 15..." (truncated timestamp)
> - **Upload Time**: "myself"
> - **Last View**: "11/12/2025, 15..." (truncated timestamp)
> - **Actions**: Star icon (favorite) and pin icon
>
> **Key Elements:**
> - **PROCESSED status**: Green indicator showing successful profile processing
> - **mlp_with_mm_kernel**: Profile name for the uploaded kernel
> - **Upload timestamp**: 11/12/2025, 15:xx
> - **Uploader**: "myself"
> - **Action icons**: Star (favorite) and pin options
> - **Filter search**: Search box to filter profiles by name
> - **Single profile**: Profile count shows (1)


* If you click on the name of your profile in Profile Name column, it will navigate to profile page


> **Figure: nki profiler 4**
>
> A screenshot of the Neuron Explorer interface showing a detailed timeline visualization of kernel execution traces with multiple component tracks and an operator performance table below.
>
> This screenshot displays the Neuron Explorer view within the Neuron Profiler application, providing comprehensive profiling analysis for the "mlp_with_mm_kernel" profile. The interface is divided into a timeline visualization at the top and a data table at the bottom.
>
> **Header and Navigation:**
> - Title: "Neuron Explorer" in the header bar
> - Profile name: "mlp_with_mm_kernel" displayed below
> - Left sidebar shows: Profile Manager, Profile (selected), Summary
> - Right side has "+ Add Widget" dropdown and "Layout" button
> - User menu shows "myself"
>
> **Search/Filter Controls:**
> - Search field with category selector
> - "Select category" dropdown
> - "Select field" dropdown  
> - Text input field ("Enter value")
> - "Submit" and "Clear result" buttons
>
> **Timeline Visualization (Device Timeline):**
> The timeline shows multiple horizontal tracks spanning from 0 to approximately 2,069,835 ns (about 2.07 ms). Each track represents a different engine or component:
>
> - qSync100(nc1): Sparse activity markers
> - qSync100(nc0): Sparse activity markers
> - qGpSimdDynamic (nc1): Activity markers visible
> - qScalarDynamic (nc1): Scattered colored markers (pink/magenta)
> - qSyncDynamicSM (nc1): Activity patterns
> - qGpSimdDynamic (nc0): Activity bursts
> - qScalarDynamic (nc0): Dense activity (pink markers)
> - qSyncDynamic (nc0): Continuous activity bar
> - Sync(nc1): Activity patterns with markers
> - Sync(nc0): Activity with markers
> - Tensor(nc1): Sparse activity
> - Tensor(nc0): Dense orange/colored bar showing tensor engine activity
> - TensorMatrix (nc1): Activity pattern
>
> The timeline shows colored bars and markers indicating when each component is active, with particularly dense activity visible in the Tensor(nc0) track (shown as orange bars).
>
> **Operator Table:**
> Below the timeline, a data table shows operator-level performance metrics with columns:
> - Node Name: xla__op+locals+CallImpl_custom-call.5, .4, .3
> - subgraph_id | subop_id: 0|45, 0|44, 0|43
> - MFU: 26.22%, 34.74%, 37.80%
> - HFU: 26.22%, 35.82%, 38.99%
> - Channels Scalar: 0, 4096, 7936
> - Channels Vector: 12288, 16384, 33024
> - Instructions Scalar: (values shown)
> - Instructions Ve... (truncated)
>
> Additional rows show:
> - aten_relu_maximum.51: 0|28, 0.00% MFU/HFU, 4096 Channels Vector
> - aten_relu_maximum.32: 0|22, 0.00% MFU/HFU, 8192 Channels Vector
>
> **Tab Selectors:**
> - Operator Table (selected)
> - Overall Summary
> - Event Details
> - Current Selection Summary
> - Annotations
>
> **Key Elements:**
> - **Timeline tracks**: Multiple engine/component activity visualization
> - **Tensor(nc0)**: Dense orange activity showing tensor engine utilization
> - **MFU/HFU columns**: Model/Hardware FLOPS Utilization percentages
> - **Custom call operators**: XLA operations being profiled
> - **Time scale**: 0 to ~2.07 ms execution window
> - **Two NeuronCores**: nc0 and nc1 tracks shown separately


* If you hover over any engine instruction in the timeline with your mouse, you will see instruction details in a pop-up box.


> **Figure: nki profiler 5**
>
> A Neuron Profiler Device Timeline view showing detailed instruction information for a MATMUL operation, including timing, source code location, and memory buffer usage tracks.
>
> This screenshot displays the Device Timeline view within the Neuron Profiler, showing execution traces across multiple NeuronCore components with a detailed popup for a selected MATMUL instruction.
>
> **Timeline Tracks (top to bottom):**
> - Tensor(nc1): Sparse activity markers
> - Tensor(nc0): Dense orange/colored activity bars showing tensor engine operations
> - TensorMatrix (nc1): Activity pattern
> - TensorMatrix (nc0): Activity bars
> - Vector(nc1): Sparse yellow activity markers
> - Vector(nc0): Yellow activity burst followed by sparse markers
> - Scalar(nc1): Dense purple activity pattern
> - Scalar(nc0): Dense purple activity pattern with visible bars
> - GpSimd(nc1): Sparse markers
> - GpSimd(nc0): Activity markers
> - State Buffer Usage(nc1): Gray utilization graph showing memory usage over time
> - State Buffer Usage(nc0): Gray utilization graph
> - PSUM Usage (nc1): Sparse activity
>
> **Detailed Instruction Popup (purple/lavender background):**
> The popup shows information for a selected Tensor operation:
> - Name: S[S] (Tensor)+x@complete acc_flags=0 fp32_mode=LOW_HIGH src=p[32*x34f7*[1,0,0][S12,1,1]] dst=0x20018001[1,0,0][S12,1,1] 128*128
> - Time: 595,748 ns - 596,339 ns
> - Duration: 591 ns
> - Opcode: MATMUL
> - Hierarchy: xla__op_CallImpl_custom-call.3
> - Instruction Type: REGULAR
> - Compiler PC: 2704
> - NKI Source Location: /home/ethschan/mlp_with_mm_kernel.py:116
> - Stack Frame ID(s): 33
>
> **Time Scale:**
> The timeline spans from 0 to approximately 2,069,835 ns (2.07 ms), with the selected operation occurring around 595-596 microseconds.
>
> **Buffer Usage Graphs:**
> The State Buffer Usage tracks show grayscale area graphs indicating memory utilization over time, with varying levels throughout the execution.
>
> **Key Elements:**
> - **MATMUL opcode**: Matrix multiplication operation highlighted
> - **591 ns duration**: Time for this specific matrix operation
> - **Source location**: Python file path and line 116 shown
> - **Tensor(nc0) track**: Dense activity showing tensor engine utilization
> - **State Buffer Usage**: Memory utilization visualization
> - **Custom-call.3**: XLA operator hierarchy reference
> - **fp32_mode=LOW_HIGH**: Floating point precision mode
> - **128*128**: Matrix dimensions for the operation


* If you click on any engine instruction in the timeline with your mouse, you will see event details in a panel below the timeline.


> **Figure: nki profiler 6**
>
> A Neuron Profiler Device Timeline view with the Event Details tab selected, showing detailed field-value pairs for a selected instruction including timing, hierarchy, and instruction metadata.
>
> This screenshot displays the Neuron Profiler interface with a timeline visualization at the top and an Event Details panel below showing comprehensive information about a selected operation.
>
> **Timeline Tracks (visible portion):**
> The upper section shows multiple component tracks including:
> - Sync(nc1), Sync(nc0): Synchronization events with colored markers
> - Tensor(nc1), Tensor(nc0): Tensor engine activity with orange bars
> - TensorMatrix tracks: Matrix operation indicators
> - Vector(nc1), Vector(nc0): Vector engine activity with yellow markers
> - Scalar(nc1), Scalar(nc0): Scalar engine activity with purple/magenta markers
> - GpSimd(nc1): GPSIMD activity
>
> A blue vertical selection indicator highlights a specific event in the timeline, with a horizontal blue bar visible in the Event Details header showing this is the selected event region.
>
> **Event Details Tab (selected):**
> The lower panel shows the "Event Details" tab selected (highlighted) among tabs for Operator Table, Overall Summary, Event Details, Current Selection Summary, and Annotations.
>
> **Field-Value Table:**
> The table displays detailed information with Field and Value columns:
> - ctv_pc: 29679
> - ctv_ind: 65535
> - duration_ns: 189
> - Engine: Tensor
> - ete_wait_time_ns: (value not visible)
> - fully_qualified_subgraph: nsp0
> - hierarchyName: xla__op+locals+CallImpl_custom-call.3
> - Nki_attrs: {"op_type":"xla__op_uls03+locals+u03eCallImpl","source_file":"/shared/ethan/nn/x_server/lib/python3.10/site-packages/torch_xla/core/xla_ops_registry.py","source_line":"44"}
> - Nki_name: %custom-call.3 = custom-call(%transpose.14, %transpose.12, %constant.4)
> - instructionId: 141955007395627936
> - instructionName: fp32_mode=LOW transpose_mode=DISABLED src=fp32@block32fhb 1,0,0|12*[1,1] 128*128
> - instructionType: REGULAR
>
> **Key Elements:**
> - **Event Details tab**: Selected view showing instruction metadata
> - **hierarchyName**: XLA operation reference (custom-call.3)
> - **Nki_attrs**: JSON attributes including source file and line information
> - **Nki_name**: Full custom-call definition with operands
> - **instructionName**: Detailed instruction parameters including fp32_mode and dimensions
> - **duration_ns: 189**: Instruction duration in nanoseconds
> - **Engine: Tensor**: Indicates this is a tensor engine operation
> - **Blue selection bar**: Shows selected event region in timeline


* To view hierarchy of this profile, click on Add Widget and select Hierarchy.


> **Figure: nki profiler 7**
>
> A screenshot of the Neuron Explorer interface showing the expanded "+ Add Widget" dropdown menu with available visualization and analysis widget options.
>
> This screenshot displays the Neuron Explorer view with the "Add Widget" dropdown menu expanded, revealing the various widget types that can be added to the profiler interface for analysis.
>
> **Header and Controls:**
> - Title: "Neuron Explorer" in header bar
> - Profile name: "mlp_with_mm_kernel"
> - Search controls with category/field selectors and Submit/Clear result buttons
> - "+ Add Widget" button (blue, expanded showing dropdown)
> - "Layout" button on the right
>
> **Add Widget Dropdown Menu:**
> The expanded dropdown shows the following widget options:
> - **Search**: Widget for searching events
> - **Hierarchy** (highlighted/selected): Shows operator hierarchy
> - **Device Timeline**: Timeline visualization of device activity
> - **Event Details**: Detailed event information view
> - **Overall Summary**: High-level performance summary
> - **Current Selection Summary**: Summary of selected events
> - **Operator Table**: Tabular view of operators
> - **Annotations**: User annotations widget
> - **Code Editor**: Source code viewing/editing widget
> - **Settings**: Configuration options
> - **AI Recommendation**: AI-powered optimization suggestions
>
> **Device Timeline (background):**
> Behind the dropdown, a partial view of the Device Timeline is visible showing tracks for:
> - qSync100(nc1) and qSync100(nc0): Sync operations
> - qGpSimdDynamic (nc1): GPSIMD dynamic operations
> - qScalarDynamic (nc0): Scalar dynamic operations with colored activity markers
> - qSyncDynamicSM (nc1): Sync dynamic state machine
> - qGpSimdDynamic (nc0): GPSIMD activity with visible blue/purple markers
> - qScalarDynamic (nc0): Activity shown with markers
>
> **Left Sidebar:**
> - Profile Manager
> - Profile (selected)
> - Summary
>
> **Key Elements:**
> - **Add Widget dropdown**: Central feature showing all available widget types
> - **Hierarchy option**: Currently highlighted/hovered option
> - **AI Recommendation**: Notable feature for AI-assisted optimization
> - **Code Editor**: Widget for viewing NKI source code
> - **Device Timeline**: Background visualization partially visible
> - **Layout button**: For arranging widgets in the interface
> - **Widget variety**: 11 different widget options available


* Using the Profiler’s flexible layout support, you can drag and group every widget into any panel of your choice to customize the layout for your workflow.


> **Figure: nki profiler 8**
>
> A Neuron Explorer screenshot showing both the Hierarchy view (top) and Device Timeline view (bottom), displaying the operator execution hierarchy alongside detailed engine-level timing traces.
>
> This screenshot presents a dual-panel layout in the Neuron Explorer, combining a high-level Hierarchy view with the detailed Device Timeline for comprehensive kernel analysis.
>
> **Hierarchy View (Top Panel):**
> The upper panel shows a hierarchical timeline visualization with a "Model" row on the left axis. The timeline displays operator execution blocks at different levels:
> - First level shows operators like "aten_mm..." and "xla__op+locals+CallImpl_custom-call.3"
> - A highlighted region shows "aten_view" 
> - Later in the timeline: "xla__op+locals+CallImpl_custom-call.4"
> - At the end: "xla__op+locals+CallImpl..." (truncated)
> - Colored blocks represent different operators: gray, cyan/teal, orange, and green blocks
>
> The hierarchy shows nested relationships between operations, with some operators containing sub-operations indicated by smaller blocks within larger ones.
>
> **Device Timeline (Bottom Panel):**
> The lower panel shows the detailed engine-level execution traces with multiple tracks:
>
> - qGpSimdDynamic (nc0): GPSIMD dynamic activity with colored markers
> - qScalarDynamic (nc0): Dense magenta/purple activity patterns
> - qSyncDynamicSM (nc0): Scattered activity markers
> - Sync(nc1) and Sync(nc0): Synchronization activity
> - Tensor(nc1): Sparse activity
> - Tensor(nc0): Dense orange bars showing tensor engine utilization
> - TensorMatrix (nc1) and (nc0): Matrix operation indicators
> - Vector(nc1) and Vector(nc0): Yellow vector engine activity
> - Scalar(nc1) and Scalar(nc0): Magenta scalar operations
> - GpSimd(nc1) and GpSimd(nc0): GPSIMD activity patterns
>
> **Time Scale:**
> Both views share a consistent time scale from 0 to approximately 2,069,035 ns (about 2.07 ms), with time markers at 200,000, 400,000, 600,000, etc.
>
> **Correlation:**
> The two views are time-aligned, allowing users to correlate high-level operator execution (Hierarchy) with low-level engine activity (Device Timeline).
>
> **Key Elements:**
> - **Hierarchy view**: Shows operator-level execution with nested relationships
> - **Device Timeline**: Shows engine-level instruction traces
> - **custom-call.3, custom-call.4**: XLA custom call operators visible in hierarchy
> - **aten_mm, aten_view**: PyTorch-originated operations
> - **Tensor(nc0)**: Dense orange activity indicating matrix operations
> - **Time alignment**: Both panels synchronized for correlation
> - **~2.07 ms total**: Full kernel execution time span


* If you right-click on an operator in the hierarchy timeline, it will highlight all related instructions in the instruction timeline.


> **Figure: nki profiler 9**
>
> A Neuron Explorer screenshot showing the Hierarchy and Device Timeline views with a detailed popup displaying operator performance metrics including MFU (Model FLOPS Utilization) for a custom-call operation.
>
> This screenshot presents the Neuron Explorer dual-panel layout with an information popup showing detailed performance metrics for a selected operator in the Hierarchy view.
>
> **Hierarchy View (Top Panel):**
> The upper panel shows the operator hierarchy timeline with:
> - "Model" label on the left axis
> - Operator blocks including "xla__op+locals+CallImpl_custom-call.3" highlighted/selected (shown in orange/golden color)
> - The selected operator spans a significant portion of the timeline
>
> **Operator Detail Popup (Golden/Yellow Background):**
> A detailed popup appears for the selected operator showing:
> - Name: xla__op_CallImpl_custom-call.3
> - Duration: 1.046ms
> - Time: 225,365ns - 1,285,725ns
> - Subgraph: 0
> - Subop ID: 43
> - MFU: 64.3%
>
> **Device Timeline (Bottom Panel):**
> The lower panel shows comprehensive engine-level traces:
> - qScalarDynamic (nc0): Dense magenta activity
> - qGpSimdDynamic: Activity markers
> - qSync: Synchronization events
> - Sync(nc1), Sync(nc0): Sparse markers
> - Tensor(nc1): Sparse activity
> - Tensor(nc0): Dense orange activity bars showing heavy tensor engine use
> - TensorMatrix (nc1), (nc0): Matrix operation traces
> - Vector(nc1): Sparse yellow markers
> - Vector(nc0): Dense yellow activity showing vector operations
> - Scalar(nc1), Scalar(nc0): Magenta scalar engine activity
> - GpSimd(nc1): Activity markers
> - GpSimd(nc0): Activity patterns
> - Additional tracks continue below
>
> **Time Scale:**
> Timeline spans from 0 to approximately 1,800,435 ns (1.8 ms), with regular time markers.
>
> **Performance Insight:**
> The 64.3% MFU indicates the custom-call operation achieves relatively good utilization of the tensor engine's theoretical peak performance. The 1.046ms duration represents the bulk of the kernel's execution time.
>
> **Key Elements:**
> - **MFU: 64.3%**: Model FLOPS Utilization for the selected operator
> - **Duration: 1.046ms**: Operator execution time
> - **custom-call.3**: XLA custom call operator selected
> - **Subop ID: 43**: Internal operator identifier
> - **Tensor(nc0) activity**: Dense orange bars correlating with the operator
> - **Vector(nc0) activity**: Yellow bars showing vector operations
> - **Time range**: 225,365ns to 1,285,725ns


### View NKI Source Code in Neuron Profile

You can optionally include your NKI source code files for display in Neuron Profile. When provided, Neuron Profile loads the source code into an integrated viewer, displayed side-by-side with the execution timeline in the web UI. This makes it easier to navigate between the instruction trace and the corresponding NKI source code, and to track the exact version of the code that generated the profile.

> **Note**
>
> Note
> 
> 
> Even if you don’t upload the source code, the NKI source filename and line number remain available in the instruction detail view as noted in View Neuron Profile UI.

* If source code is uploaded with NEFF and NTFF file, you will be able to see the source code in the code editor. To open the code editor, click on **Add Widget** and select **Code Editor**.


> **Figure: nki profiler 10**
>
> A Neuron Explorer screenshot showing the expanded Add Widget dropdown menu with the "Code Editor" option highlighted, demonstrating how to add a source code viewer to the profiling interface.
>
> This screenshot displays the Neuron Explorer interface with the Add Widget dropdown menu expanded, focusing on the Code Editor option for viewing NKI source code alongside profiling data.
>
> **Header and Layout:**
> - Title: "Neuron Explorer"
> - Profile: "mlp_with_mm_kernel"
> - "+ Add Widget" button expanded (blue dropdown arrow)
> - "Layout" button on the right
> - User: "myself"
>
> **Add Widget Dropdown Menu:**
> The expanded menu shows widget options with "Code Editor" highlighted (indicated by darker/selected background):
> - Search
> - Hierarchy
> - Device Timeline
> - Event Details
> - Overall Summary
> - Current Selection Summary
> - Operator Table
> - Annotations
> - **Code Editor** (highlighted/selected)
> - Settings
> - AI Recommendation
>
> **Hierarchy View:**
> Below the menu, the Hierarchy timeline is visible showing:
> - "Model" row with operator blocks
> - "aten_zero_..." block at the start
> - "xla__op+locals+CallImpl_custom-call.3" (orange block)
> - "aten_p..." (cyan/teal block)
> - "xla__op+locals+CallImpl_custom-call" continuing on the right
> - "aten_v..." and "aten_..." blocks (colored in teal and green)
>
> **Device Timeline (Partial View):**
> At the bottom, a partial view of the Device Timeline shows:
> - "qGpSimdDynamic (nc0)" track with blue/colored activity markers
>
> **Time Scale:**
> The visible timeline spans from 0 to approximately 1,856,613 ns, with markers at 200,000, 400,000, 600,000, 800,000, 1,000,000, 1,200,000, and 1,400,000.
>
> **Key Elements:**
> - **Code Editor option**: Highlighted widget selection for viewing source code
> - **Add Widget menu**: Full list of 11 available widgets
> - **Hierarchy view**: Shows operator-level execution timeline
> - **custom-call.3**: Main NKI kernel operator visible
> - **aten_* operations**: Framework operations surrounding the custom call
> - **Integration purpose**: Code Editor enables viewing NKI source alongside traces


* The code editor will be open on the right-hand side.


> **Figure: nki profiler 11**
>
> A Neuron Explorer screenshot showing a three-panel layout with Hierarchy view, Device Timeline, and Code Editor displaying the NKI kernel source code for matrix multiplication.
>
> This screenshot presents a comprehensive profiling view combining timeline visualization with source code inspection, enabling correlation between code and execution behavior.
>
> **Code Editor Panel (Right Side):**
> The Code Editor widget displays the NKI kernel source file "mlp_with_mm_kernel.py" with Python code visible:
>
> ```python
> # Example 3-layer MLP with matrix multiplication kernel
>
> import torch
> import torch_xla
> import torch_xla.core.xla_model as xm
> import torch.nn.functional as F
> import torch
>
> import nki.language as nl
> import nki.isa as nisa
>
> import os
>
> os.environ["NEURON_FRAMEWORK_DEBUG"] = "1"
> os.environ["XLA_HLO_DEBUG"] = "1"
> os.environ["NEURON_PLATFORM_TARGET_OVERRIDE"] = "trn2"
>
> @nki.jit
> def matmul(x, kernel):
>     # NKI parameters
>     TILES_IN_BLOCK_M=12,
>     TILES_IN_BLOCK_K=8,
>     TILES_IN_BLOCK_N=4,
>
>     """MM kernel to compute a large matrix multiplication.
>     Blocking all dimensions and using layout optimiza...
>
> Args:
>     lhs: an input tensor of shape [M,K], where M is...
>          TILES_IN_BLOCK_M and K is a multiple of 128 X...
>          left-hand-side argument of the matrix multip...
>          for optimal performance.
>     rhs: an input tensor of shape [K,N], where K is...
>          TILES_IN_BLOCK_K and N is a multiple of 512 X...
>          the right-hand-side argument of the matrix mu...
>          TILES_IN_BLOCK_N x 512 is required for optima...
> Returns:
>     result: the resulting output tensor of shape 2K...
>
> M, K = lhs.shape
> ```
>
> The code shows the kernel function definition with NKI decorators, imports, and docstring describing matrix multiplication parameters.
>
> **Hierarchy View (Top Left):**
> Shows the operator hierarchy with:
> - "Model" row containing operator blocks
> - custom-call operations and aten operations visible
>
> **Device Timeline (Bottom Left):**
> Shows multiple engine tracks:
> - qScalarDynamic, qGpSimdDynamic: Activity markers
> - Sync, Tensor, TensorMatrix, Vector, Scalar, GpSimd tracks
> - Dense orange bars in Tensor(nc0) and yellow in Vector(nc0)
>
> **File Selector:**
> Above the code editor: "EXPLORER" tab with "mlp_with_mm_kernel.py" file selected
>
> **Key Elements:**
> - **Code Editor widget**: Displays NKI source code
> - **@nki.jit decorator**: Platform target "trn2" visible
> - **TILES_IN_BLOCK_***: Tiling parameters for the matmul kernel
> - **Matrix dimensions**: M, K, N blocking strategy described
> - **Source correlation**: Code visible alongside execution traces
> - **mlp_with_mm_kernel.py**: The profiled NKI kernel file
> - **Three-panel layout**: Hierarchy + Timeline + Code Editor


* Hover on an instruction that has NKI source location and **Command + left click** on Mac (**Ctrl + right click** on Windows), and it will pop-up a window for showing file selection for stack trace.


> **Figure: nki profiler 12**
>
> A Neuron Profiler popup dialog showing "Select Source Location" with a list of source code line references from the mlp_with_mm_kernel.py file, allowing users to navigate to specific code locations.
>
> This screenshot shows a dark-themed modal dialog that appears when navigating between source code locations in the Neuron Profiler. The dialog enables jumping to specific lines in the NKI kernel source code that correspond to profiled instructions.
>
> **Dialog Header:**
> - Title: "Select Source Location"
> - Instructions: "Use up/down arrows to navigate, Enter to select, Esc to cancel"
>
> **Source Location Options:**
> The dialog presents five selectable source locations, each showing a file path and line number:
>
> 1. **mlp_with_mm_kernel.py:116** (highlighted/selected with blue background)
>    - Subtitle: mlp_with_mm_kernel.py
>
> 2. **mlp_with_mm_kernel.py:157**
>    - Subtitle: mlp_with_mm_kernel.py
>
> 3. **mlp_with_mm_kernel.py:169**
>    - Subtitle: mlp_with_mm_kernel.py
>
> 4. **mlp_with_mm_kernel.py:184**
>    - Subtitle: mlp_with_mm_kernel.py
>
> 5. **mlp_with_mm_kernel.py:192**
>    - Subtitle: mlp_with_mm_kernel.py
>
> **Background Context:**
> Behind the dialog, portions of the timeline view are visible showing colored activity bars (orange and blue/purple) near the top, and the time scale at the bottom showing values like 0,000, 1,200,000, 1,600,000, and 2,069,035 (nanoseconds).
>
> **Selection State:**
> The first option (line 116) is currently selected/highlighted with a blue background, indicating it will be the destination if Enter is pressed.
>
> **Key Elements:**
> - **Select Source Location dialog**: Navigation modal for jumping to code lines
> - **Line 116**: First and currently selected location (likely main kernel code)
> - **Line 157, 169, 184, 192**: Additional source locations referenced by profile
> - **mlp_with_mm_kernel.py**: The NKI kernel source file
> - **Keyboard navigation**: Arrow keys to select, Enter to confirm, Esc to cancel
> - **Multiple references**: Shows that profiled instructions map to multiple source lines


* Selecting any option from the list, it will jump to the line of the source code and highlight all of instructions related to this line.


> **Figure: nki profiler 13**
>
> A Neuron Explorer screenshot showing the Code Editor with highlighted NKI kernel code, displaying loop structures and tensor operations with a minimap view of the full source file on the right side.
>
> This screenshot shows the Neuron Explorer with three panels: Hierarchy view (top left), Device Timeline (bottom left), and Code Editor (right side) showing detailed NKI kernel implementation code.
>
> **Code Editor Panel (Right Side):**
> The Code Editor displays a section of mlp_with_mm_kernel.py with NKI kernel code visible. The code shows nested loop structures and tensor operations:
>
> ```python
> for m in range(NUM_BLOCK_M):
>     for k in range(NUM_BLOCK_K):
>         for bk_i in range(TILES_IN_BLOCK_K):
>             lhs_tile = nl.load(lhs_tensor[bk_i+TILE_K, K:K+BLOCK,
>                               size=mm_copy]
>             for n in range(NUM_BLOCK_N):
>                 rhs_tile = nisa.nc_transpose(
>                     result_tile = ...
>                     stationary=lhs_tile,
>                     moving=rhs_tile,tile_id=lhs_tile.tile_id)
>             movingbits_tile=tile[bk_i:M]
>
>     # Accumulate the result into the ...
>     nisa.tensor_tensor(data=result_tile,
>                        dataPresult_tile,
>                        ...)
>
> # Copying the result from SBUF to HBM
> for m in range(NUM_BLOCK_M):
>     result_packed = shuf.simd...
>     for bn in range(...)
> ```
>
> A yellow/gold highlighted line indicates the currently selected or referenced source location. The code shows NKI-specific constructs including:
> - `range()` for loop iterations
> - `nisa.dma_copy()` for DMA operations
> - `nisa.nc_transpose()` for tensor transposition
> - `nisa.tensor_tensor()` for tensor accumulation
>
> **Minimap (Far Right):**
> A code minimap shows a zoomed-out view of the entire source file, with a highlighted region indicating the current viewport position.
>
> **Hierarchy View (Top Left):**
> Shows the model execution hierarchy with operator blocks.
>
> **Device Timeline (Bottom Left):**
> Shows engine-level traces with:
> - Dense activity in Scalar and Tensor tracks
> - Yellow/orange bars indicating tensor engine operations
> - Multiple sync and dynamic operation tracks
>
> **Key Elements:**
> - **range()**: NKI loop construct
> - **nisa.dma_copy()**: DMA load operation
> - **nisa.nc_transpose()**: Tensor transposition instruction
> - **nisa.tensor_tensor()**: Tensor-tensor accumulation
> - **Highlighted line**: Currently selected source location
> - **Code minimap**: Overview of full source file
> - **Loop nesting**: Shows M, K, N blocking structure for matmul


* You can also enable different source code decorations in **Source Code Settings**.


> **Figure: nki profiler 14**
>
> A Neuron Profiler Settings panel showing Source Code Settings with toggle options for navigation, decorations, and display preferences for NKI and PyTorch source code visualization.
>
> This screenshot displays the Settings panel within the Neuron Profiler, specifically showing the "Source Code Settings" tab with various configuration options for how source code is displayed and navigated in the profiler interface.
>
> **Tab Bar:**
> The top shows two closeable tabs:
> - "Instruction X" (closeable)
> - "Settings X" (currently active, highlighted in blue)
> - Window control icons (expand/close) on the right
>
> **Left Navigation Panel:**
> Three settings categories listed vertically:
> - Display Settings
> - **Source Code Settings** (selected, highlighted with blue background and left border)
> - Timeline Settings
>
> **Source Code Settings Options:**
>
> **Top-Level Options (with toggle switches):**
> 1. **Source Code Time Range Decorations** (toggle: OFF)
>    - Likely shows time information inline with source code
>
> 2. **Source Code Lowest Level Navigation** (toggle: OFF)
>    - Controls navigation granularity to lowest-level instructions
>
> **Source Code Navigation Section:**
> Two toggle options for framework-specific navigation:
> - **NKI** (toggle: ON, blue filled)
> - **PyTorch** (toggle: ON, blue filled)
>
> **Source Code Decorations Section:**
> Four toggle options controlling what information is shown in the code editor:
> - **InstructionCount** (toggle: OFF)
> - **FLOPS** (toggle: OFF)
> - **Clicked** (toggle: ON, blue filled)
> - **Dependencies** (toggle: ON, blue filled)
>
> **Toggle States:**
> - Blue filled toggle = ON/enabled
> - Gray/empty toggle = OFF/disabled
>
> The settings allow users to customize the profiler experience by:
> - Enabling/disabling source code navigation for different frameworks
> - Showing/hiding performance metrics inline with code
> - Controlling visual decorations and dependency visualization
>
> **Key Elements:**
> - **Source Code Settings tab**: Currently selected settings category
> - **NKI toggle**: Enable NKI source code navigation (ON)
> - **PyTorch toggle**: Enable PyTorch source code navigation (ON)
> - **InstructionCount/FLOPS**: Optional performance decorations (OFF)
> - **Clicked/Dependencies toggles**: Enable click highlighting and dependency visualization (ON)
> - **Time Range Decorations**: Show time information in code (OFF)


> **Figure: nki profiler 15**
>
> A Neuron Profiler Code Editor view showing NKI kernel source code with inline performance decorations displaying FLOPS counts, instruction counts, and engine breakdown information for specific code lines.
>
> This screenshot shows the Code Editor widget in the Neuron Profiler displaying detailed NKI kernel code with performance annotations (decorations) overlaid on specific source lines.
>
> **File Explorer Panel (Left):**
> - "EXPLORER" header
> - "mlp_with_mm_kernel.py" file selected
>
> **Code Editor (Main Area):**
> The editor displays NKI kernel code with the following visible structure:
>
> ```python
> for m in range(NUM_BLOCK_M):
>     for k in range(NUM_BLOCK_K):
>         for bk_i in range(TILES_IN_BLOCK_K):
>             lhs_tile = i|
>             for n in range(NUM_BLOCK_N):
>                 lhs_tile = nl.load(lhs_tensor[...], shape=[TILE_K, BLOCK_M])
>                 nisa.dma_copy(
>                     arr=lhs_tile[...:TILE_K, K:BLOCK_M],
>                     src=lhs[TILES_IN_BLOCK_M * ... + bk_i * 1 + ...
>                     ... (TILES_IN_BLOCK_K * ... + bk_i ... ) + TILE_K,
>                     BLOCK_M + m*BLOCK_M * m + 1])
>                 lhs_f_tiles.append(lhs_tile)
> ```
>
> **Performance Decorations (Inline Annotations):**
> Yellow/highlighted decorations appear on specific lines showing:
>
> 1. **For the nisa.dma_copy section:**
>    - "# Do matmul with all tiles in the blocks"
>    - FLOPS: 17179869184
>    - InstructionCount: 4,096
>    - Engine Breakdown:
>    - - Tensor: 4,096 instructions(100%) nc_range(TILES_IN_BLOCK_N):
>
> 2. **For nisa.nc_matmul:**
>    - Shows "nisa.nc_matmul("
>    - "dst=result_tile,"
>    - "stationary=lhs_f_tiles[bk_i][TILE_K, bn ="
>    - "TILE_K*[bm * 1 + TILE_M,"
>    - "moving=rhs_tiles[bk_i][TILE_K, bn * 1 + TILE_N]"
>
> 3. **For nisa.tensor_tensor section:**
>    - "# Accumulate the result into the result_tmp tile."
>    - "nisa.tensor_tensor(data=result_tile[bm:bm+bm],"
>    - "data0=result_tmp_tile[bm:bm:bm],"
>    - "data=result_tile,"
>    - "op0=nl.add)"
>
> **Minimap (Right Edge):**
> A code minimap shows the full file structure with highlighted regions indicating the current viewport position.
>
> **Key Elements:**
> - **FLOPS: 17179869184**: Total floating-point operations for the matmul
> - **InstructionCount: 4,096**: Number of tensor instructions
> - **Engine Breakdown: Tensor 100%**: All instructions execute on tensor engine
> - **nisa.nc_matmul**: NKI matrix multiplication instruction
> - **nisa.tensor_tensor**: Tensor accumulation operation
> - **range**: NKI loop construct
> - **Inline decorations**: Yellow highlighted performance metrics
> - **Loop structure**: M, K, N blocking visible in code


## Next Steps

Great! Now that you’ve learned how to profile an NKI kernel, it’s time to take this further:

* Dive into the NKI Performance Guide to discover techniques for making your kernels faster and more efficient.

* Explore the [NKI sample kernels](https://github.com/aws-neuron/nki-samples) to see real-world examples of high-performance kernel implementations — and get inspiration for your own NKI kernels.

By combining profiling insights with optimization strategies and practical examples, you’ll be well-equipped to write NKI kernels that leverage Neuron hardware in an efficient way.