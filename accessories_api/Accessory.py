class Accessory:
    def __init__(self):
        raise NotImplementedError()
    
    def initialize(self):
        raise NotImplementedError()
    
    def shutdown(self):
        raise NotImplementedError()
    
    def isFaulted(self):
        raise NotImplementedError()