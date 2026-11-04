# NKI and Neuron Architecture

NKI and Neuron Architecture
NKI currently supports the following NeuronDevice generations:

* Trainium/Inferentia2, available on AWS `trn1`, `trn1n` and `inf2` instances

* Trainium2, available on AWS `trn2` instances and UltraServers

* Trainium3, available on AWS `trn3` instances and UltraServers

The documents below provide an architecture deep dive of each NeuronDevice generation,
with a focus on areas that NKI developers can directly control through kernel implementation.

* [Trainium/Inferentia2 Architecture Guide](trainium_inferentia2_arch.md) serves as a foundational architecture guide for understanding the basics of any NeuronDevice generation.

* [Trainium2 Architecture Guide](trainium2_arch.md) walks through the architecture enhancements when compared to the previous generation.

* [Trainium3 Architecture Guide](trainium3_arch.md) covers the enhancements for the next-generation Trainium ML accelerators.

Neuron recommends new NKI developers start with [Trainium/Inferentia2 Architecture Guide](trainium_inferentia2_arch.md) before exploring newer NeuronDevice architecture.

[Trainium/Inferentia2 Architecture Guide](trainium_inferentia2_arch.md#trainium-inferentia2-arch)
Foundational architecture guide for understanding NeuronDevice basics.

[Trainium2 Architecture Guide](trainium2_arch.md#trainium2-arch)
Architecture enhancements and improvements in the Trainium2 generation.

[Trainium3 Architecture Guide](trainium3_arch.md#trainium3-arch)
Latest architecture features and capabilities in Trainium3 devices.