import torch.nn
from models.archs.SPSR_arch import ImageGradientNoPadding
from data.weight_scheduler import get_scheduler_for_opt
from torch.utils.checkpoint import checkpoint
#from models.steps.recursive_gen_injectors import ImageFlowInjector

# Injectors are a way to sythesize data within a step that can then be used (and reused) by loss functions.
def create_injector(opt_inject, env):
    type = opt_inject['type']
    if type == 'generator':
        return ImageGeneratorInjector(opt_inject, env)
    elif type == 'discriminator':
        return DiscriminatorInjector(opt_inject, env)
    elif type == 'scheduled_scalar':
        return ScheduledScalarInjector(opt_inject, env)
    elif type == 'img_grad':
        return ImageGradientInjector(opt_inject, env)
    elif type == 'add_noise':
        return AddNoiseInjector(opt_inject, env)
    elif type == 'greyscale':
        return GreyInjector(opt_inject, env)
    elif type == 'interpolate':
        return InterpolateInjector(opt_inject, env)
    elif type == 'imageflow':
        return ImageFlowInjector(opt_inject, env)
    else:
        raise NotImplementedError


class Injector(torch.nn.Module):
    def __init__(self, opt, env):
        super(Injector, self).__init__()
        self.opt = opt
        self.env = env
        if 'in' in opt.keys():
            self.input = opt['in']
        self.output = opt['out']

    # This should return a dict of new state variables.
    def forward(self, state):
        raise NotImplementedError


# Uses a generator to synthesize an image from [in] and injects the results into [out]
# Note that results are *not* detached.
class ImageGeneratorInjector(Injector):
    def __init__(self, opt, env):
        super(ImageGeneratorInjector, self).__init__(opt, env)

    def forward(self, state):
        gen = self.env['generators'][self.opt['generator']]
        if isinstance(self.input, list):
            params = [state[i] for i in self.input]
            results = gen(*params)
        else:
            results = gen(state[self.input])
        new_state = {}
        if isinstance(self.output, list):
            # Only dereference tuples or lists, not tensors.
            assert isinstance(results, list) or isinstance(results, tuple)
            for i, k in enumerate(self.output):
                new_state[k] = results[i]
        else:
            new_state[self.output] = results

        return new_state


# Injects a result from a discriminator network into the state.
class DiscriminatorInjector(Injector):
    def __init__(self, opt, env):
        super(DiscriminatorInjector, self).__init__(opt, env)

    def forward(self, state):
        d = self.env['discriminators'][self.opt['discriminator']]
        if isinstance(self.input, list):
            params = [state[i] for i in self.input]
            results = d(*params)
        else:
            results = d(state[self.input])
        new_state = {}
        if isinstance(self.output, list):
            # Only dereference tuples or lists, not tensors.
            assert isinstance(results, list) or isinstance(results, tuple)
            for i, k in enumerate(self.output):
                new_state[k] = results[i]
        else:
            new_state[self.output] = results

        return new_state


# Creates an image gradient from [in] and injects it into [out]
class ImageGradientInjector(Injector):
    def __init__(self, opt, env):
        super(ImageGradientInjector, self).__init__(opt, env)
        self.img_grad_fn = ImageGradientNoPadding().to(env['device'])

    def forward(self, state):
        return {self.opt['out']: self.img_grad_fn(state[self.opt['in']])}


# Injects a scalar that is modulated with a specified schedule. Useful for increasing or decreasing the influence
# of something over time.
class ScheduledScalarInjector(Injector):
    def __init__(self, opt, env):
        super(ScheduledScalarInjector, self).__init__(opt, env)
        self.scheduler = get_scheduler_for_opt(opt['scheduler'])

    def forward(self, state):
        return {self.opt['out']: self.scheduler.get_weight_for_step(self.env['step'])}


# Adds gaussian noise to [in], scales it to [0,[scale]] and injects into [out]
class AddNoiseInjector(Injector):
    def __init__(self, opt, env):
        super(AddNoiseInjector, self).__init__(opt, env)

    def forward(self, state):
        # Scale can be a fixed float, or a state key (e.g. from ScheduledScalarInjector).
        if isinstance(self.opt['scale'], str):
            scale = state[self.opt['scale']]
        else:
            scale = self.opt['scale']

        noise = torch.randn_like(state[self.opt['in']], device=self.env['device']) * scale
        return {self.opt['out']: state[self.opt['in']] + noise}


# Averages the channel dimension (1) of [in] and saves to [out]. Dimensions are
# kept the same, the average is simply repeated.
class GreyInjector(Injector):
    def __init__(self, opt, env):
        super(GreyInjector, self).__init__(opt, env)

    def forward(self, state):
        mean = torch.mean(state[self.opt['in']], dim=1, keepdim=True)
        mean = mean.repeat(1, 3, 1, 1)
        return {self.opt['out']: mean}

import torchvision.utils as utils
class InterpolateInjector(Injector):
    def __init__(self, opt, env):
        super(InterpolateInjector, self).__init__(opt, env)

    def forward(self, state):
        scaled = torch.nn.functional.interpolate(state[self.opt['in']], scale_factor=self.opt['scale_factor'],
                                                 mode=self.opt['mode'])
        return {self.opt['out']: scaled}
